"""Prospect discovery over pluggable sources (GitHub, GitLab).

Scans the configured topics on each enabled source, groups repositories by
owner, ranks the owners that look like *active builders* (the ones most likely
to sponsor or co-market a plugin), and optionally locates a public contact
address from the owner profile or their public website.

Sources implement the small `ProspectSource` surface - `search_repositories`,
`get_owner`, `find_public_email` - and return GitHub-shaped repository dicts,
so aggregation, filtering and ranking are source-agnostic. Multi-source runs
merge candidates by normalized owner name.

No scraping of private data: only the public REST APIs and the owner's own
published website. Contacts are surfaced for a human to confirm before use.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

try:  # pragma: no cover - import guard
    import requests
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "The 'requests' package is required. Run: pip install -r requirements.txt"
    ) from exc

from logging_setup import get_logger
from models import (
    OWNER_ORG,
    OWNER_USER,
    PROSPECT_SOURCES,
    DiscoveredSponsor,
    is_valid_email,
    looks_like_noise_email,
    normalize_name,
)

LOG = get_logger("discovery")

DEFAULT_API_BASE = "https://api.github.com"
DEFAULT_GITLAB_API_BASE = "https://gitlab.com/api/v4"
_EMAIL_IN_TEXT = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}",
    re.IGNORECASE,
)
_JUNK_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".woff", ".woff2")
_NON_HTML_SUFFIXES = (".pdf", ".zip", ".xml", ".json", ".css", ".js")


class DiscoveryError(Exception):
    """Raised when a prospect source cannot be reached or the token is invalid."""


# --------------------------------------------------------------------------- #
# The source contract
# --------------------------------------------------------------------------- #


class ProspectSource(Protocol):
    """What every discovery source must offer.

    Repositories are returned in the GitHub project shape (`owner.login`,
    `stargazers_count`, ...) so the aggregator never needs to know which API
    they came from; owners are returned in the shape `find_public_email`
    expects (`email`, `website`, `blog`, `twitter_username`).
    """

    name: str

    def search_repositories(self, topic: str, *, per_page: int = 100, max_pages: int = 3) -> List[Dict[str, Any]]:
        ...

    def get_owner(self, login: str) -> Dict[str, Any]:
        ...

    def find_public_email(self, owner: Dict[str, Any], *, max_sites: int = 3) -> Tuple[str, str]:
        ...


# --------------------------------------------------------------------------- #
# Shared plumbing
# --------------------------------------------------------------------------- #


def _seconds_until(reset_header: Optional[str]) -> int:
    try:
        reset_at = int(reset_header or 0)
    except (TypeError, ValueError):
        return 60
    return max(0, reset_at - int(time.time()))


def fetch_public_html(
    session: requests.Session,
    url: str,
    *,
    user_agent: str,
    timeout: int,
    max_bytes: int = 400_000,
) -> str:
    """Fetch a public HTML page, capped in size. Best effort."""
    if not url.lower().startswith(("http://", "https://")):
        return ""
    try:
        response = session.get(
            url,
            timeout=min(timeout, 15),
            headers={"User-Agent": user_agent},
            allow_redirects=True,
            stream=True,
        )
        if response.status_code >= 400:
            LOG.debug("website fetch %s -> HTTP %s", url, response.status_code)
            return ""
        content = response.raw.read(max_bytes, decode_content=True) or b""
        return content.decode(response.encoding or "utf-8", errors="replace")
    except requests.RequestException as exc:
        LOG.debug("website fetch %s failed: %s", url, exc)
        return ""


def extract_email_from_html(html: str) -> str:
    """Pull the most plausible public contact address out of an HTML page."""
    if not html:
        return ""
    text = html.replace("\n", " ")
    found: List[str] = []
    for match in _EMAIL_IN_TEXT.findall(text):
        email = match.strip().strip(".,;:!?'\"").lower()
        if not is_valid_email(email) or looks_like_noise_email(email):
            continue
        if any(email.endswith(extension) for extension in _JUNK_EXTENSIONS):
            continue
        if len(email) > 254 or email.count("@") != 1:
            continue
        found.append(email)
    # Prefer role/contact addresses over personal ones when both exist.
    for email in found:
        if email.split("@", 1)[0] in {"contact", "info", "hello", "sales", "partnerships", "support"}:
            return email
    return found[0] if found else ""


def scrape_public_email(
    owner: Dict[str, Any],
    fetch: Any,
    *,
    max_sites: int = 3,
    profile_label: str = "profile",
) -> Tuple[str, str]:
    """Best-effort public contact address. Returns ``(email, source)``.

    ``owner`` uses the shared normalized shape: `email`, `website`, `blog`,
    `twitter_username`. ``fetch(url) -> html`` is the caller's page fetcher.
    """
    api_email = (owner.get("email") or "").strip().lower()
    if api_email and is_valid_email(api_email) and not looks_like_noise_email(api_email):
        return api_email, profile_label

    candidates: List[str] = []
    for field in ("website", "blog"):
        value = (owner.get(field) or "").strip()
        if value:
            candidates.append(value if value.startswith("http") else f"https://{value}")
    if owner.get("twitter_username"):
        candidates.append(f"https://x.com/{owner['twitter_username']}")

    checked = 0
    for url in dict.fromkeys(candidates):
        if checked >= max_sites:
            break
        if url.rstrip("/").lower().endswith(_NON_HTML_SUFFIXES):
            continue
        checked += 1
        email = extract_email_from_html(fetch(url))
        if email:
            return email, url
    return "", ""


# --------------------------------------------------------------------------- #
# GitHub source
# --------------------------------------------------------------------------- #


@dataclass
class GitHubClient:
    """Thin, polite wrapper around the parts of the GitHub REST API we need."""

    token: str = ""
    api_base: str = DEFAULT_API_BASE
    user_agent: str = "GenesysPluginSponsorBot/1.0"
    timeout: int = 20
    delay: float = 0.4
    token_env: str = "GITHUB_TOKEN"
    session: Optional[requests.Session] = None

    name = "github"

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": self.user_agent,
            }
        )
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"
        self.token_env = (self.token_env or "GITHUB_TOKEN").strip() or "GITHUB_TOKEN"
        self.api_base = self.api_base.rstrip("/")

    # -- plumbing ---------------------------------------------------------- #

    def _sleep(self) -> None:
        if self.delay > 0:
            time.sleep(self.delay)

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = path if path.startswith("http") else f"{self.api_base}{path}"
        self._sleep()
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise DiscoveryError(f"request to {url} failed: {exc}") from exc

        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining == "0":
            reset = response.headers.get("X-RateLimit-Reset")
            wait = _seconds_until(reset)
            raise DiscoveryError(
                "GitHub API rate limit exhausted "
                f"(resets in ~{wait}s). Set ${self.token_env} to raise it."
            )
        if response.status_code == 401:
            raise DiscoveryError(
                f"GitHub rejected the credentials (401). Check ${self.token_env}."
            )
        if response.status_code == 403:
            raise DiscoveryError(
                "GitHub returned 403 (rate limit or blocked). "
                "Set GITHUB_TOKEN to a personal access token and retry."
            )
        if response.status_code == 404:
            LOG.debug("404 for %s", url)
            return {}
        if response.status_code >= 400:
            raise DiscoveryError(f"GitHub returned HTTP {response.status_code} for {url}")
        try:
            return response.json()
        except ValueError as exc:
            raise DiscoveryError(f"invalid JSON from {url}: {exc}") from exc

    @staticmethod
    def _seconds_until(reset_header: Optional[str]) -> int:
        return _seconds_until(reset_header)

    @property
    def rate_limit(self) -> Dict[str, Any]:
        """Current rate-limit window (cheap, unauthenticated-friendly)."""
        url = f"{self.api_base}/rate_limit"
        try:
            payload = self.session.get(url, timeout=self.timeout).json()
        except (requests.RequestException, ValueError) as exc:
            raise DiscoveryError(f"could not read rate limit: {exc}") from exc
        return payload.get("resources", {}).get("search", {})

    # -- API surface ------------------------------------------------------- #

    def search_repositories(self, topic: str, *, per_page: int = 100, max_pages: int = 3) -> List[Dict[str, Any]]:
        """All repos tagged with ``topic`` (up to ``max_pages`` x per_page)."""
        clean_topic = topic.strip().lstrip("#")
        if not clean_topic:
            return []
        collected: List[Dict[str, Any]] = []
        for page in range(1, max(1, max_pages) + 1):
            payload = self._get(
                "/search/repositories",
                params={"q": f"topic:{clean_topic}", "per_page": per_page, "page": page},
            )
            items = payload.get("items") or []
            if not isinstance(items, list):
                break
            collected.extend(item for item in items if isinstance(item, dict))
            total = int(payload.get("total_count") or 0)
            LOG.info(
                "topic:%s page %d -> %d repos (+%d total seen, %d indexed)",
                clean_topic,
                page,
                len(items),
                len(collected),
                total,
            )
            if len(items) < per_page or len(collected) >= min(total, per_page * max_pages):
                break
        return collected

    def get_owner(self, login: str) -> Dict[str, Any]:
        return self._get(f"/users/{login}")

    def get_authenticated_user(self) -> Dict[str, Any]:
        """The token owner's profile, or ``{}`` when running anonymously.

        Used by the web UI's "connect to GitHub" step to prove a token works.
        """
        if not self.token:
            return {}
        return self._get("/user")

    def fetch_page(self, url: str, *, max_bytes: int = 400_000) -> str:
        """Fetch a public HTML page, capped in size. Best effort."""
        self._sleep()
        return fetch_public_html(
            self.session, url, user_agent=self.user_agent, timeout=self.timeout, max_bytes=max_bytes
        )

    # -- email discovery --------------------------------------------------- #

    def find_public_email(self, owner: Dict[str, Any], *, max_sites: int = 3) -> Tuple[str, str]:
        """Best-effort public contact address. Returns ``(email, source)``."""
        return scrape_public_email(
            owner, self.fetch_page, max_sites=max_sites, profile_label="github-profile"
        )


# --------------------------------------------------------------------------- #
# GitLab source
# --------------------------------------------------------------------------- #


@dataclass
class GitLabClient:
    """The parts of the GitLab REST API that mirror what discovery needs.

    Search returns projects tagged with the topic; results are normalized to
    the GitHub project shape so the aggregator is source-agnostic. Owner
    lookups try the user namespace first, then the group namespace.
    """

    token: str = ""
    api_base: str = DEFAULT_GITLAB_API_BASE
    user_agent: str = "GenesysPluginSponsorBot/1.0"
    timeout: int = 20
    delay: float = 0.4
    token_env: str = "GITLAB_TOKEN"
    session: Optional[requests.Session] = None

    name = "gitlab"

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": self.user_agent})
        if self.token:
            self.session.headers["PRIVATE-TOKEN"] = self.token
        self.token_env = (self.token_env or "GITLAB_TOKEN").strip() or "GITLAB_TOKEN"
        self.api_base = self.api_base.rstrip("/")
        self._last_rate: Dict[str, Any] = {}

    # -- plumbing ---------------------------------------------------------- #

    def _sleep(self) -> None:
        if self.delay > 0:
            time.sleep(self.delay)

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = path if path.startswith("http") else f"{self.api_base}{path}"
        self._sleep()
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise DiscoveryError(f"request to {url} failed: {exc}") from exc

        remaining = response.headers.get("RateLimit-Remaining")
        if remaining is not None:
            self._last_rate = {
                "limit": response.headers.get("RateLimit-Limit", ""),
                "remaining": remaining,
                "reset": response.headers.get("RateLimit-Reset", ""),
            }
        if remaining == "0":
            wait = _seconds_until(response.headers.get("RateLimit-Reset"))
            raise DiscoveryError(
                f"GitLab API rate limit exhausted (resets in ~{wait}s). "
                f"Set ${self.token_env} to raise it."
            )
        if response.status_code == 401:
            raise DiscoveryError(
                f"GitLab rejected the credentials (401). Check ${self.token_env}."
            )
        if response.status_code == 403:
            raise DiscoveryError(
                "GitLab returned 403 (forbidden or blocked). "
                "Set GITLAB_TOKEN to a personal access token and retry."
            )
        if response.status_code == 429:
            wait = _seconds_until(response.headers.get("RateLimit-Reset"))
            raise DiscoveryError(
                f"GitLab rate limited the client (429, resets in ~{wait}s). "
                f"Set ${self.token_env} to raise it."
            )
        if response.status_code == 404:
            LOG.debug("404 for %s", url)
            return None
        if response.status_code >= 400:
            raise DiscoveryError(f"GitLab returned HTTP {response.status_code} for {url}")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise DiscoveryError(f"invalid JSON from {url}: {exc}") from exc

    @property
    def rate_limit(self) -> Dict[str, Any]:
        """Last-seen rate-limit window (GitLab reports it on every response)."""
        return dict(self._last_rate)

    # -- normalization ----------------------------------------------------- #

    @staticmethod
    def _normalize_project(project: Dict[str, Any]) -> Dict[str, Any]:
        namespace = project.get("namespace") or {}
        kind = str(namespace.get("kind") or "user").lower()
        return {
            "id": project.get("id"),
            "full_name": project.get("path_with_namespace") or "",
            "html_url": project.get("web_url") or "",
            "description": project.get("description") or "",
            "stargazers_count": int(project.get("star_count") or 0),
            "forks_count": int(project.get("forks_count") or 0),
            "language": "",  # the list API does not report languages
            "topics": [topic for topic in (project.get("topics") or []) if isinstance(topic, str)],
            "fork": bool(project.get("forked_from_project")),
            "owner": {
                "login": namespace.get("path") or "",
                "type": OWNER_ORG if kind == "group" else OWNER_USER,
            },
        }

    @staticmethod
    def _normalize_user(user: Dict[str, Any]) -> Dict[str, Any]:
        website = (user.get("website_url") or "").strip()
        return {
            "login": user.get("username") or "",
            "type": OWNER_USER,
            "email": (user.get("public_email") or "").strip().lower(),
            "blog": website,
            "website": website,
            "twitter_username": user.get("twitter") or "",
            "location": user.get("location") or "",
            "html_url": user.get("web_url") or "",
            "name": user.get("name") or "",
            "public_repos": 0,  # not exposed; the topic-repo count is used instead
        }

    @staticmethod
    def _normalize_group(group: Dict[str, Any]) -> Dict[str, Any]:
        website = (group.get("website_url") or "").strip()
        return {
            "login": group.get("path") or "",
            "type": OWNER_ORG,
            "email": "",
            "blog": website,
            "website": website,
            "twitter_username": "",
            "location": group.get("location") or "",
            "html_url": group.get("web_url") or "",
            "name": group.get("name") or "",
            "description": group.get("description") or "",
            "public_repos": 0,
        }

    # -- API surface ------------------------------------------------------- #

    def search_repositories(self, topic: str, *, per_page: int = 100, max_pages: int = 3) -> List[Dict[str, Any]]:
        """Public projects tagged with ``topic``, normalized to GitHub shape."""
        clean_topic = topic.strip().lstrip("#")
        if not clean_topic:
            return []
        collected: List[Dict[str, Any]] = []
        for page in range(1, max(1, max_pages) + 1):
            payload = self._get(
                "/projects",
                params={
                    "topic": clean_topic,
                    "visibility": "public",
                    "order_by": "star_count",
                    "sort": "desc",
                    "per_page": per_page,
                    "page": page,
                },
            )
            items = payload if isinstance(payload, list) else []
            collected.extend(self._normalize_project(item) for item in items if isinstance(item, dict))
            LOG.info(
                "gitlab topic:%s page %d -> %d repos (+%d indexed)",
                clean_topic,
                page,
                len(items),
                len(collected),
            )
            if len(items) < per_page:
                break
        return collected

    def get_owner(self, login: str) -> Dict[str, Any]:
        """User or group profile, normalized to the shared owner shape."""
        clean = (login or "").strip().lstrip("@")
        if not clean:
            return {}
        user = self._get(f"/users/{clean}")
        if isinstance(user, dict) and user.get("username"):
            return self._normalize_user(user)
        group = self._get(f"/groups/{clean}")
        if isinstance(group, dict) and group.get("path"):
            return self._normalize_group(group)
        return {}

    def fetch_page(self, url: str, *, max_bytes: int = 400_000) -> str:
        """Fetch a public HTML page, capped in size. Best effort."""
        self._sleep()
        return fetch_public_html(
            self.session, url, user_agent=self.user_agent, timeout=self.timeout, max_bytes=max_bytes
        )

    def find_public_email(self, owner: Dict[str, Any], *, max_sites: int = 3) -> Tuple[str, str]:
        """Best-effort public contact address. Returns ``(email, source)``."""
        return scrape_public_email(
            owner, self.fetch_page, max_sites=max_sites, profile_label="gitlab-profile"
        )


# --------------------------------------------------------------------------- #
# Aggregation (source-agnostic)
# --------------------------------------------------------------------------- #


@dataclass
class _OwnerBucket:
    login: str
    owner_type: str = OWNER_USER
    repos_count: int = 0
    stars: int = 0
    forks: int = 0
    best_stars: int = -1
    top_repo: str = ""
    top_repo_url: str = ""
    description: str = ""
    language: str = ""
    repo_ids: set = None  # type: ignore[assignment]
    topics: List[str] = None  # type: ignore[assignment]
    discovered_from: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.repo_ids is None:
            self.repo_ids = set()
        if self.topics is None:
            self.topics = []
        if self.discovered_from is None:
            self.discovered_from = []

    def add_repo(self, repo: Dict[str, Any], topic: str) -> None:
        stars = int(repo.get("stargazers_count") or 0)
        repo_id = str(repo.get("id") or repo.get("full_name") or "")
        if repo_id and repo_id in self.repo_ids:
            # Same repo matched via two topics - count it once.
            if topic not in self.discovered_from:
                self.discovered_from.append(topic)
            return
        if repo_id:
            self.repo_ids.add(repo_id)
        self.repos_count += 1
        self.stars += stars
        self.forks += int(repo.get("forks_count") or 0)
        if stars > self.best_stars:
            self.best_stars = stars
            self.top_repo = str(repo.get("full_name") or "")
            self.top_repo_url = str(repo.get("html_url") or "")
            self.description = str(repo.get("description") or "")
            self.language = str(repo.get("language") or "")
        for existing in repo.get("topics") or []:
            if isinstance(existing, str) and existing not in self.topics:
                self.topics.append(existing)
        if topic not in self.discovered_from:
            self.discovered_from.append(topic)


def aggregate_repositories(repos_by_topic: Dict[str, Sequence[Dict[str, Any]]]) -> Dict[str, _OwnerBucket]:
    """Group repositories into per-owner buckets, keyed by normalized login."""
    buckets: Dict[str, _OwnerBucket] = {}
    for topic, repos in repos_by_topic.items():
        for repo in repos:
            if not isinstance(repo, dict):
                continue
            owner = repo.get("owner") or {}
            login = str(owner.get("login") or "").strip()
            if not login:
                continue
            key = normalize_name(login)
            bucket = buckets.get(key)
            if bucket is None:
                bucket = _OwnerBucket(login=login, owner_type=str(owner.get("type") or OWNER_USER))
                buckets[key] = bucket
            bucket.add_repo(repo, topic)
    return buckets


def passes_filter(candidate: DiscoveredSponsor, *, min_org_repos: int, min_individual_genesys_repos: int) -> bool:
    """Orgs need total repos, individuals need Genesys-topic repos."""
    if candidate.is_org:
        return candidate.repos_count >= min_org_repos
    return candidate.genesys_repos_count >= min_individual_genesys_repos


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def discover_sponsors(
    source: ProspectSource,
    topics: Iterable[str],
    *,
    min_org_repos: int = 5,
    min_individual_genesys_repos: int = 3,
    min_stars: int = 0,
    exclude_forks: bool = True,
    per_page: int = 100,
    max_pages: int = 3,
    max_owner_lookups: int = 40,
    scrape_public_emails: bool = True,
    email_scrape_max_sites: int = 3,
) -> List[DiscoveredSponsor]:
    """Run the full discovery sweep on one source and return qualifying candidates."""
    topic_list = [topic.strip().lstrip("#") for topic in topics if topic and topic.strip()]
    if not topic_list:
        raise DiscoveryError("no topics configured (github.topics)")

    source_name = getattr(source, "name", "github")
    profile_base = "https://gitlab.com/" if source_name == "gitlab" else "https://github.com/"

    repos_by_topic: Dict[str, List[Dict[str, Any]]] = {}
    for topic in topic_list:
        LOG.info("Scanning topic:%s on %s", topic, source_name)
        try:
            repos = source.search_repositories(topic, per_page=per_page, max_pages=max_pages)
        except DiscoveryError as exc:
            LOG.error("topic:%s failed - %s", topic, exc)
            continue
        if exclude_forks:
            repos = [repo for repo in repos if not repo.get("fork")]
        repos_by_topic[topic] = repos

    buckets = aggregate_repositories(repos_by_topic)
    LOG.info("Aggregated %d repos across %d owners", sum(len(v) for v in repos_by_topic.values()), len(buckets))

    # Build candidates that already look promising, without hitting the API yet.
    preliminary: List[DiscoveredSponsor] = []
    for bucket in buckets.values():
        if bucket.stars < min_stars:
            continue
        preliminary.append(
            DiscoveredSponsor(
                owner_name=bucket.login,
                owner_type=bucket.owner_type,
                github_url=f"{profile_base}{bucket.login}",
                repos_count=bucket.repos_count,
                genesys_repos_count=bucket.repos_count,
                top_repo=bucket.top_repo,
                top_repo_url=bucket.top_repo_url,
                stars=bucket.stars,
                forks=bucket.forks,
                description=bucket.description,
                language=bucket.language,
                topics=list(bucket.topics),
                discovered_from=list(bucket.discovered_from),
                source=source_name,
            )
        )

    preliminary.sort(key=lambda item: (-item.stars, -item.genesys_repos_count, item.owner_name.lower()))

    # Owner profile lookups give the real repo count + public email. Budgeted.
    lookups = 0
    for candidate in preliminary:
        if lookups >= max_owner_lookups:
            LOG.info("Owner lookup budget (%d) reached", max_owner_lookups)
            break
        owner = source.get_owner(candidate.owner_name)
        if not owner:
            continue
        lookups += 1
        candidate.owner_type = str(owner.get("type") or candidate.owner_type)
        candidate.repos_count = int(owner.get("public_repos") or candidate.repos_count)
        candidate.location = str(owner.get("location") or "")
        candidate.blog = str(owner.get("blog") or "")
        candidate.website = str(owner.get("website") or "")
        profile_url = str(owner.get("html_url") or "")
        if profile_url:
            candidate.github_url = profile_url
        if scrape_public_emails:
            email, email_via = source.find_public_email(owner, max_sites=email_scrape_max_sites)
            if email:
                candidate.email_if_public = email
                candidate.email_source = email_via

    if len(preliminary) > lookups:
        LOG.info(
            "%d owner(s) not profiled (lookup budget %d); their org filter is evaluated "
            "against topic-tagged repos only",
            len(preliminary) - lookups,
            max_owner_lookups,
        )

    qualified = [
        candidate
        for candidate in preliminary
        if passes_filter(
            candidate,
            min_org_repos=min_org_repos,
            min_individual_genesys_repos=min_individual_genesys_repos,
        )
    ]
    qualified.sort(key=lambda item: (-item.stars, -item.genesys_repos_count, item.owner_name.lower()))
    LOG.info(
        "Discovery complete: %d/%d owners passed the filter (org>=%d repos, individual>=%d genesys repos)",
        len(qualified),
        len(preliminary),
        min_org_repos,
        min_individual_genesys_repos,
    )
    return qualified


def merge_candidates(groups: Sequence[Sequence[DiscoveredSponsor]]) -> List[DiscoveredSponsor]:
    """Merge per-source candidate lists by normalized owner name.

    The same owner found on two sources becomes one candidate: counts take the
    maximum, topic lists are unioned, and ``source`` records every platform
    that produced it (``github+gitlab``). GitHub-derived fields (profile URL,
    top repo) win ties so existing tracker notes stay accurate.
    """
    merged: Dict[str, DiscoveredSponsor] = {}
    for group in groups:
        for candidate in group:
            existing = merged.get(candidate.key)
            if existing is None:
                merged[candidate.key] = candidate
                continue
            if existing.source == "gitlab" and candidate.source == "github":
                # Re-run the merge with GitHub as the surviving record.
                existing, candidate = candidate, existing
                merged[candidate.key] = existing
            existing.stars = max(existing.stars, candidate.stars)
            existing.forks = max(existing.forks, candidate.forks)
            existing.repos_count = max(existing.repos_count, candidate.repos_count)
            existing.genesys_repos_count = max(
                existing.genesys_repos_count, candidate.genesys_repos_count
            )
            if existing.source != candidate.source:
                existing.source = "+".join(
                    part for part in ("github", "gitlab") if part in (existing.source, candidate.source)
                )
            for topic in candidate.topics:
                if topic not in existing.topics:
                    existing.topics.append(topic)
            for origin in candidate.discovered_from:
                if origin not in existing.discovered_from:
                    existing.discovered_from.append(origin)
            if not existing.email_if_public and candidate.email_if_public:
                existing.email_if_public = candidate.email_if_public
                existing.email_source = candidate.email_source
            if not existing.github_url and candidate.github_url:
                existing.github_url = candidate.github_url
            if not existing.top_repo and candidate.top_repo:
                existing.top_repo = candidate.top_repo
                existing.top_repo_url = candidate.top_repo_url
            for field in ("website", "blog", "location", "description", "language"):
                if not getattr(existing, field) and getattr(candidate, field):
                    setattr(existing, field, getattr(candidate, field))
    merged_list = list(merged.values())
    merged_list.sort(key=lambda item: (-item.stars, -item.genesys_repos_count, item.owner_name.lower()))
    return merged_list


def discover_from_sources(
    sources: Sequence[ProspectSource],
    topics: Iterable[str],
    *,
    merge: bool = True,
    **kwargs: Any,
) -> List[DiscoveredSponsor]:
    """Sweep every source; a failing source is logged and skipped (fail-soft)."""
    groups: List[List[DiscoveredSponsor]] = []
    for source in sources:
        source_name = getattr(source, "name", "source")
        try:
            groups.append(discover_sponsors(source, topics, **kwargs))
        except DiscoveryError as exc:
            LOG.error("discovery source %s failed - %s", source_name, exc)
    if not groups:
        return []
    if len(groups) == 1 or not merge:
        merged = [candidate for group in groups for candidate in group]
        merged.sort(key=lambda item: (-item.stars, -item.genesys_repos_count, item.owner_name.lower()))
        return merged
    return merge_candidates(groups)


# --------------------------------------------------------------------------- #
# Source construction
# --------------------------------------------------------------------------- #


def client_from_config(config) -> GitHubClient:  # noqa: ANN001 - avoid import cycle
    """Build a GitHubClient from a `Config` instance."""
    return GitHubClient(
        token=config.github_token,
        api_base=config.str("github.api_base", DEFAULT_API_BASE),
        user_agent=config.str("github.user_agent", "GenesysPluginSponsorBot/1.0"),
        timeout=config.int("github.timeout_seconds", 20, minimum=5, maximum=120),
        delay=config.float("github.request_delay_seconds", 0.4),
        token_env=config.str("github.token_env", "GITHUB_TOKEN"),
    )


def gitlab_client_from_config(config) -> GitLabClient:  # noqa: ANN001 - avoid import cycle
    """Build a GitLabClient from a `Config` instance."""
    return GitLabClient(
        token=config.gitlab_token,
        api_base=config.str("gitlab.api_base", DEFAULT_GITLAB_API_BASE),
        user_agent=config.str("gitlab.user_agent", config.str("github.user_agent", "GenesysPluginSponsorBot/1.0")),
        timeout=config.int("gitlab.timeout_seconds", 20, minimum=5, maximum=120),
        delay=config.float("gitlab.request_delay_seconds", 0.4),
        token_env=config.str("gitlab.token_env", "GITLAB_TOKEN"),
    )


def sources_from_config(config, only: Optional[Sequence[str]] = None) -> List[ProspectSource]:  # noqa: ANN001
    """Build the discovery sources named in config (or ``only``, e.g. CLI --source).

    ``only=["all"]`` expands to every known source. Unknown names raise, since
    a silent skip would look like an empty sweep.
    """
    if only is not None:
        requested = [str(item).strip().lower() for item in only if str(item).strip()]
        if "all" in requested:
            requested = list(PROSPECT_SOURCES)
    else:
        requested = config.discovery_sources
    unknown = [name for name in requested if name not in PROSPECT_SOURCES]
    if unknown:
        raise DiscoveryError(
            f"unknown discovery source(s): {', '.join(unknown)} "
            f"(known: {', '.join(PROSPECT_SOURCES)})"
        )
    built: List[ProspectSource] = []
    seen: List[str] = []
    for name in requested:
        if name in seen:
            continue
        seen.append(name)
        if name == "github":
            built.append(client_from_config(config))
        elif name == "gitlab":
            built.append(gitlab_client_from_config(config))
    return built


# --------------------------------------------------------------------------- #
# CLI helpers
# --------------------------------------------------------------------------- #


def render_table(candidates: Sequence[DiscoveredSponsor], *, width: int = 3) -> str:
    """Human readable candidate listing for the CLI."""
    if not candidates:
        return "No candidates found."
    header = (
        f"{'#':>2}  {'OWNER':<32} {'TYPE':<12} {'SRC':<13} {'REPOS':>5} {'GEN':>4} "
        f"{'STARS':>6}  {'EMAIL':<34} TOP REPO"
    )
    lines = [header, "-" * len(header)]
    for index, candidate in enumerate(candidates, start=1):
        email = candidate.email_if_public or "-"
        owner = candidate.owner_name[:32]
        lines.append(
            f"{index:>2}  {owner:<32} {candidate.owner_type:<12} {candidate.source[:13]:<13} "
            f"{candidate.repos_count:>5} {candidate.genesys_repos_count:>4} "
            f"{candidate.stars:>6}  {email[:34]:<34} {(candidate.top_repo or '-')[:width * 12]}"
        )
    return "\n".join(lines)


def split_selection(raw: str, count: int) -> List[int]:
    """Parse ``--add`` input: ``1,3-5,all`` -> zero-based indexes."""
    selection: List[int] = []
    text = (raw or "").strip().lower()
    if not text:
        return selection
    for chunk in text.replace(" ", "").split(","):
        if not chunk:
            continue
        if chunk in {"all", "*"}:
            return list(range(count))
        if "-" in chunk:
            start_raw, _, end_raw = chunk.partition("-")
            try:
                start, end = int(start_raw), int(end_raw)
            except ValueError:
                continue
            if start > end:
                start, end = end, start
            selection.extend(range(start - 1, end))
        else:
            try:
                selection.append(int(chunk) - 1)
            except ValueError:
                continue
    return sorted({index for index in selection if 0 <= index < count})
