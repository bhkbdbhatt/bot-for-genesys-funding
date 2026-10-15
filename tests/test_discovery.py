"""Pluggable discovery sources: GitHub, GitLab, merging and source selection."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from discovery import (
    DiscoveryError,
    GitHubClient,
    GitLabClient,
    aggregate_repositories,
    discover_from_sources,
    discover_sponsors,
    extract_email_from_html,
    merge_candidates,
    sources_from_config,
)
from models import DiscoveredSponsor


# --------------------------------------------------------------------------- #
# Fake HTTP plumbing (no live network anywhere in this module)
# --------------------------------------------------------------------------- #


class FakeRaw:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, size: int = -1, decode_content: bool = False) -> bytes:
        return self._body[:size] if size and size >= 0 else self._body


class FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        payload: Any = None,
        text: str = "",
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.status_code = status
        self._payload = payload
        self._text = text
        self.headers = headers or {}
        self.encoding = "utf-8"
        self.content = b"x" if (payload is not None or text) else b""
        self.raw = FakeRaw(text.encode("utf-8"))

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class FakeSession:
    """Routes GET calls to a caller-supplied handler and records them."""

    def __init__(self) -> None:
        self.headers: Dict[str, str] = {}
        self.calls: List[Dict[str, Any]] = []

    def get(self, url: str, params: Any = None, timeout: Any = None, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, "params": params, "kwargs": kwargs})
        return self.handler(url, params)

    def handler(self, url: str, params: Any) -> FakeResponse:  # pragma: no cover - overridden
        raise AssertionError(f"unexpected GET {url}")


def make_session(handler) -> FakeSession:
    session = FakeSession()
    session.handler = handler  # type: ignore[assignment]
    return session


# --------------------------------------------------------------------------- #
# Email scraping helper
# --------------------------------------------------------------------------- #


def test_extract_email_prefers_role_addresses():
    html = "ping jane@acme.dev or better contact@acme.dev"
    assert extract_email_from_html(html) == "contact@acme.dev"


def test_extract_email_ignores_noise_domains():
    html = "write noreply@example.com or real@acme.dev"
    assert extract_email_from_html(html) == "real@acme.dev"


# --------------------------------------------------------------------------- #
# GitHub source (unchanged behavior)
# --------------------------------------------------------------------------- #


def _github_repo(repo_id, login, stars, *, fork=False, topics=("genesys-cloud",)):
    return {
        "id": repo_id,
        "full_name": f"{login}/repo-{repo_id}",
        "html_url": f"https://github.com/{login}/repo-{repo_id}",
        "description": "a plugin",
        "language": "Python",
        "stargazers_count": stars,
        "forks_count": 1,
        "topics": list(topics),
        "fork": fork,
        "owner": {"login": login, "type": "User"},
    }


def test_github_search_pages_and_stops_at_short_page():
    def handler(url, params):
        page = params["page"]
        items = [_github_repo(page * 100 + i, "acme", 10) for i in range(2 if page == 2 else 100)]
        return FakeResponse(payload={"items": items, "total_count": 102})

    session = make_session(handler)
    client = GitHubClient(token="", delay=0, session=session)
    repos = client.search_repositories("genesys-cloud", per_page=100, max_pages=3)
    assert len(repos) == 102
    assert [call["params"]["page"] for call in session.calls] == [1, 2]


def test_github_client_sends_bearer_when_token_present():
    client = GitHubClient(token="ghp_secret", session=FakeSession())
    assert client.session.headers["Authorization"] == "Bearer ghp_secret"
    assert client.session.headers["Accept"] == "application/vnd.github+json"


# --------------------------------------------------------------------------- #
# GitLab source
# --------------------------------------------------------------------------- #


def _gitlab_project(pid, namespace, kind, stars, *, forked=False, topics=("genesys-cloud",)):
    return {
        "id": pid,
        "path_with_namespace": f"{namespace}/proj-{pid}",
        "web_url": f"https://gitlab.com/{namespace}/proj-{pid}",
        "description": "a plugin",
        "star_count": stars,
        "forks_count": 2,
        "topics": list(topics),
        "namespace": {"path": namespace, "kind": kind},
        "forked_from_project": {"id": 1} if forked else None,
    }


def test_gitlab_search_normalizes_to_github_shape():
    def handler(url, params):
        assert params["topic"] == "genesys-cloud"
        assert params["visibility"] == "public"
        return FakeResponse(payload=[_gitlab_project(7, "acme", "group", 42)])

    session = make_session(handler)
    client = GitLabClient(token="", delay=0, session=session)
    repos = client.search_repositories("genesys-cloud")
    assert len(repos) == 1
    repo = repos[0]
    assert repo["full_name"] == "acme/proj-7"
    assert repo["html_url"] == "https://gitlab.com/acme/proj-7"
    assert repo["stargazers_count"] == 42
    assert repo["topics"] == ["genesys-cloud"]
    assert repo["fork"] is False
    assert repo["owner"] == {"login": "acme", "type": "Organization"}


def test_gitlab_marks_forked_projects():
    def handler(url, params):
        return FakeResponse(payload=[_gitlab_project(8, "jane", "user", 1, forked=True)])

    client = GitLabClient(token="", delay=0, session=make_session(handler))
    repos = client.search_repositories("genesys-cloud")
    assert repos[0]["fork"] is True
    assert repos[0]["owner"]["type"] == "User"


def test_gitlab_search_stops_when_page_is_short():
    def handler(url, params):
        return FakeResponse(payload=[_gitlab_project(params["page"], "acme", "group", 5)])

    session = make_session(handler)
    client = GitLabClient(token="", delay=0, session=session)
    client.search_repositories("genesys-cloud", per_page=100, max_pages=3)
    assert len(session.calls) == 1


def test_gitlab_token_uses_private_token_header():
    client = GitLabClient(token="glpat_secret", session=FakeSession())
    assert client.session.headers["PRIVATE-TOKEN"] == "glpat_secret"


def test_gitlab_get_owner_prefers_user_then_group():
    def handler(url, params):
        if url.endswith("/users/jane"):
            return FakeResponse(payload={"username": "jane", "name": "Jane", "website_url": "https://jane.dev"})
        if url.endswith("/users/acme"):
            return FakeResponse(status=404)
        if url.endswith("/groups/acme"):
            return FakeResponse(payload={"path": "acme", "name": "Acme", "web_url": "https://gitlab.com/acme"})
        raise AssertionError(url)

    session = make_session(handler)
    client = GitLabClient(token="", delay=0, session=session)
    user = client.get_owner("jane")
    assert user["type"] == "User"
    assert user["website"] == "https://jane.dev"
    group = client.get_owner("acme")
    assert group["type"] == "Organization"


def test_gitlab_get_owner_missing_returns_empty():
    def handler(url, params):
        return FakeResponse(status=404, payload=None)

    client = GitLabClient(token="", delay=0, session=make_session(handler))
    assert client.get_owner("nobody") == {}


def test_gitlab_find_public_email_uses_profile_then_website():
    def handler(url, params):
        if url.startswith("https://") and "/api/" not in url:
            return FakeResponse(status=200, text="reach me at hello@jane.dev")
        raise AssertionError(url)

    session = make_session(handler)
    client = GitLabClient(token="", delay=0, session=session)
    email, via = client.find_public_email(
        {"email": "public@jane.dev", "website": "", "blog": "", "twitter_username": ""}
    )
    assert (email, via) == ("public@jane.dev", "gitlab-profile")

    email, via = client.find_public_email(
        {"email": "", "website": "https://jane.dev", "blog": "", "twitter_username": ""}
    )
    assert email == "hello@jane.dev"
    assert via == "https://jane.dev"


def test_gitlab_rate_limit_reads_response_headers():
    def handler(url, params):
        return FakeResponse(
            payload=[],
            headers={"RateLimit-Limit": "2000", "RateLimit-Remaining": "1999", "RateLimit-Reset": "1700000000"},
        )

    client = GitLabClient(token="", delay=0, session=make_session(handler))
    client.search_repositories("genesys-cloud")
    assert client.rate_limit["remaining"] == "1999"


def test_gitlab_429_raises_discovery_error():
    def handler(url, params):
        return FakeResponse(status=429, headers={"RateLimit-Reset": "1700000000"})

    client = GitLabClient(token="", delay=0, session=make_session(handler))
    with pytest.raises(DiscoveryError, match="rate limited"):
        client.search_repositories("genesys-cloud")


# --------------------------------------------------------------------------- #
# Aggregation and filtering via a stub source
# --------------------------------------------------------------------------- #


class StubSource:
    name = "stub"

    def __init__(self, repos: Dict[str, List[Dict[str, Any]]], owners: Optional[Dict[str, Dict[str, Any]]] = None):
        self.repos = repos
        self.owners = owners or {}
        self.searched: List[str] = []

    def search_repositories(self, topic, *, per_page=100, max_pages=3):
        self.searched.append(topic)
        return list(self.repos.get(topic, []))

    def get_owner(self, login):
        return dict(self.owners.get(login, {}))

    def find_public_email(self, owner, *, max_sites=3):
        return owner.pop("_email", ("", ""))


def test_aggregate_repositories_counts_each_repo_once():
    repos = {
        "genesys-cloud": [_github_repo(1, "acme", 10)],
        "genesys": [_github_repo(1, "acme", 10), _github_repo(2, "acme", 5)],
    }
    buckets = aggregate_repositories(repos)
    assert len(buckets) == 1
    bucket = buckets["acme"]
    assert bucket.repos_count == 2
    assert bucket.stars == 15
    assert sorted(bucket.discovered_from) == ["genesys", "genesys-cloud"]


def test_discover_sponsors_filters_orgs_and_individuals():
    repos = {
        "genesys-cloud": [
            _github_repo(1, "bigorg", 100),
            _github_repo(2, "solo", 3),
        ]
    }
    owners = {
        "bigorg": {"type": "Organization", "public_repos": 30},
        "solo": {"type": "User", "public_repos": 4},
    }
    source = StubSource(repos, owners)
    candidates = discover_sponsors(
        source,
        ["genesys-cloud"],
        min_org_repos=10,
        min_individual_genesys_repos=1,
        max_owner_lookups=10,
    )
    names = {candidate.owner_name for candidate in candidates}
    assert names == {"bigorg", "solo"}
    by_name = {candidate.owner_name: candidate for candidate in candidates}
    assert by_name["bigorg"].github_url == "https://github.com/bigorg"
    assert by_name["bigorg"].source == "stub"


def test_discover_sponsors_drops_candidates_below_filter():
    repos = {"genesys-cloud": [_github_repo(1, "tinyorg", 1)]}
    owners = {"tinyorg": {"type": "Organization", "public_repos": 2}}
    source = StubSource(repos, owners)
    assert discover_sponsors(source, ["genesys-cloud"], min_org_repos=10) == []


def test_discover_sponsors_skips_forks():
    repos = {"genesys-cloud": [_github_repo(1, "acme", 50, fork=True)]}
    source = StubSource(repos)
    assert discover_sponsors(source, ["genesys-cloud"], min_stars=0) == []


def test_discover_sponsors_enriches_email_from_owner():
    repos = {"genesys-cloud": [_github_repo(1, "acme", 50)]}
    owners = {"acme": {"type": "Organization", "public_repos": 50, "_email": ("hi@acme.dev", "acme.dev")}}
    source = StubSource(repos, owners)
    candidates = discover_sponsors(source, ["genesys-cloud"], min_org_repos=10)
    assert candidates[0].email_if_public == "hi@acme.dev"
    assert candidates[0].email_source == "acme.dev"


# --------------------------------------------------------------------------- #
# Merging and multi-source orchestration
# --------------------------------------------------------------------------- #


def _candidate(owner, *, source, stars, topics, repos=1, email="", url=""):
    return DiscoveredSponsor(
        owner_name=owner,
        owner_type="Organization",
        github_url=url or f"https://{source}.example.com/{owner}",
        repos_count=repos,
        genesys_repos_count=repos,
        stars=stars,
        topics=list(topics),
        discovered_from=list(topics),
        source=source,
        email_if_public=email,
    )


def test_merge_candidates_combines_two_sources():
    github = [_candidate("acme", source="github", stars=100, topics=["genesys-cloud"], url="https://github.com/acme")]
    gitlab = [_candidate("acme", source="gitlab", stars=40, topics=["genesys"], email="hi@acme.dev")]

    merged = merge_candidates([github, gitlab])
    assert len(merged) == 1
    candidate = merged[0]
    assert candidate.source == "github+gitlab"
    assert candidate.stars == 100
    assert set(candidate.topics) == {"genesys-cloud", "genesys"}
    assert candidate.email_if_public == "hi@acme.dev"
    assert candidate.github_url == "https://github.com/acme"


def test_merge_candidates_keeps_distinct_owners():
    merged = merge_candidates(
        [
            [_candidate("acme", source="github", stars=1, topics=["genesys-cloud"])],
            [_candidate("other", source="gitlab", stars=2, topics=["genesys"])],
        ]
    )
    assert {candidate.owner_name for candidate in merged} == {"acme", "other"}


def test_discover_from_sources_is_fail_soft():
    good = StubSource({"genesys-cloud": [_github_repo(1, "acme", 50)]}, {"acme": {"type": "Organization", "public_repos": 50}})

    class BrokenSource(StubSource):
        name = "broken"

        def search_repositories(self, topic, *, per_page=100, max_pages=3):
            raise DiscoveryError("boom")

    candidates = discover_from_sources(
        [good, BrokenSource({})],
        ["genesys-cloud"],
        min_org_repos=1,
        max_owner_lookups=5,
    )
    assert [candidate.owner_name for candidate in candidates] == ["acme"]


# --------------------------------------------------------------------------- #
# Source selection from config
# --------------------------------------------------------------------------- #


def test_sources_from_config_defaults_to_github(config_factory):
    config = config_factory("disc-default")
    names = [source.name for source in sources_from_config(config)]
    assert names == ["github"]


def test_sources_from_config_reads_both(config_factory):
    def mutate(document):
        document.setdefault("discovery", {})["sources"] = ["github", "gitlab"]

    config = config_factory("disc-both", mutate=mutate)
    names = [source.name for source in sources_from_config(config)]
    assert names == ["github", "gitlab"]


def test_sources_from_config_all_expands(config_factory):
    config = config_factory("disc-all")
    names = [source.name for source in sources_from_config(config, only=["all"])]
    assert names == ["github", "gitlab"]


def test_sources_from_config_rejects_unknown(config_factory):
    config = config_factory("disc-bad")
    with pytest.raises(DiscoveryError, match="unknown discovery source"):
        sources_from_config(config, only=["bitbucket"])
