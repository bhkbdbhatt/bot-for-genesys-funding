"""Publisher payload shaping (never transmitted) + dry-run socket tripwire.

Ported from _selftest.py. All network paths are stubbed; credentials are fake.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

import pytest
import yaml

from config_loader import Config
from models import PLATFORM_IDS, ContentItem
from platforms import PLATFORMS
from publishers import get_publisher
from webapp.service import content_data_from_config

CANONICAL = "https://blog.example.com/posts/stop-refreshing-tokens"

#: The seven targets that shipped before the registry grew; Phase 2+ targets
#: get their own focused tests and are only generically probed here.
LEGACY_SEVEN = ("devto", "hashnode", "medium", "wordpress", "coderlegion", "devdojo", "webhook")

FAKE_ENV = {
    "DEVTO_API_KEY": "fake-devto-key",
    "HASHNODE_PAT": "fake-hashnode-pat",
    "MEDIUM_TOKEN": "fake-medium-token",
    "WP_USERNAME": "fake-user",
    "WP_APP_PASSWORD": "fake-app-password",
    "WEBHOOK_URL": "https://example.invalid/hook",
}


@pytest.fixture()
def payload_config(tmp_path, repo_root, sample_content):
    doc = yaml.safe_load((repo_root / "config.example.yaml").read_text(encoding="utf-8"))
    doc["content"].update(
        {
            "enabled": True,
            "audience": sample_content["audience"],
            "angle": sample_content["angle"],
            "disclosure": sample_content["disclosure"],
            "canonical_base_url": CANONICAL,
        }
    )
    for pid in PLATFORM_IDS:
        doc.setdefault("platforms", {}).setdefault(pid, {})["enabled"] = True
    doc["platforms"]["devto"]["state_published"] = True
    doc["platforms"]["hashnode"]["publication_id"] = "65d99cd9447d15d98a2fc264"
    doc["platforms"]["wordpress"]["site"] = "example.wordpress.com"
    doc["platforms"]["medium"]["author_id"] = "abc123"
    doc["rate_limits"]["max_publishes_per_day"] = 50
    doc["rate_limits"]["min_hours_between_platform_posts"] = 0
    doc.setdefault("paths", {})
    doc["paths"]["content_file"] = "payload-content.json"
    doc["paths"]["tracker_file"] = "payload-sponsors.json"
    target = tmp_path / "config.yaml"
    target.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return Config.load(target, create_from_example=False)


@pytest.fixture()
def article(composed_draft):
    item = ContentItem(
        id="payload-one",
        title="Stop refreshing tokens by hand",
        body_markdown=composed_draft["body"],
        summary=composed_draft["summary"],
        platforms=list(PLATFORM_IDS),
        tags=["genesys", "genesys-cloud", "opentelemetry", "opensource", "cloud", "extra1", "extra2"],
        canonical_url=CANONICAL,
    )
    return item


@pytest.fixture()
def shaped(payload_config, article):
    content_cfg = content_data_from_config(payload_config)
    return {
        pid: content_engine_prepare(content_cfg, article, pid) for pid in PLATFORM_IDS
    }


def content_engine_prepare(content_cfg, article, pid):
    import content_engine as ce

    return ce.prepare_for_platform(
        content_cfg,
        title=article.title,
        body=article.body_markdown,
        summary=article.summary,
        tags=article.tags,
        platform=pid,
    )


@pytest.fixture()
def fake_creds(monkeypatch):
    for name, value in FAKE_ENV.items():
        monkeypatch.setenv(name, value)


def test_every_registry_id_has_a_dry_run_safe_publisher(payload_config, article, shaped, fake_creds):
    """Generic contract for all platforms, past and future."""
    for pid in PLATFORM_IDS:
        probe = get_publisher(pid, payload_config, dry_run=True)
        opened: list[str] = []

        def tripwire(*_args, _pid=pid, **_kwargs):
            opened.append(_pid)
            raise AssertionError(f"{_pid} attempted a network call during a dry run")

        probe._request = tripwire  # type: ignore[method-assign]
        probe.publish(article, shaped[pid])
        assert not opened, str(opened)


def test_devto_payload(payload_config, article, shaped, fake_creds):
    devto = get_publisher("devto", payload_config, dry_run=True)
    payload = devto.build_article(article, shaped["devto"])
    assert set(payload) >= {"title", "body_markdown", "published", "tags"}
    assert payload["body_markdown"] == article.body_markdown
    assert len(payload["tags"]) == PLATFORMS["devto"].tag_limit, str(payload["tags"])
    assert payload.get("canonical_url") == CANONICAL
    assert bool(payload.get("description"))
    assert payload["published"] is True
    assert shaped["devto"]["content_format"] == "markdown"
    assert "fake-devto-key" not in str(payload)
    assert devto.publish(article, shaped["devto"]).dry_run is True

    draft_devto = get_publisher("devto", payload_config, dry_run=True)
    draft_devto.state_published = False
    assert draft_devto.build_article(article, shaped["devto"])["published"] is False


def test_hashnode_payload(payload_config, article, shaped, fake_creds):
    hashnode = get_publisher("hashnode", payload_config, dry_run=True)
    hn = hashnode.build_input(article, shaped["hashnode"])
    assert hn["publicationId"] == "65d99cd9447d15d98a2fc264"
    assert hn["contentMarkdown"] == article.body_markdown
    assert all(isinstance(tag, dict) and "slug" in tag for tag in hn["tags"])
    assert len(hn["tags"]) == PLATFORMS["hashnode"].tag_limit, str(hn["tags"])
    assert hn.get("originalArticleURL") == CANONICAL
    assert hn.get("enableToc") is True
    assert "fake-hashnode-pat" not in str(hn)


def test_wordpress_payload(payload_config, article, shaped, fake_creds):
    wordpress = get_publisher("wordpress", payload_config, dry_run=True)
    wp = wordpress.build_post(article, shaped["wordpress"])
    assert "<h1>" in wp["content"] and "\n## " not in wp["content"]
    assert "<script" not in wp["content"].lower()
    assert '<a href="https://github.com/purecloudlabs/community-plugin-example">' in wp["content"]
    assert wp["title"] == article.title and wp["status"] == "publish"
    assert isinstance(wp["tags"], str) and "," in wp["tags"]
    assert wordpress.endpoint().endswith("/sites/example.wordpress.com/posts/new"), wordpress.endpoint()
    assert wordpress.auth_headers()["Authorization"].startswith("Basic ")

    self_hosted = get_publisher("wordpress", payload_config, dry_run=True)
    self_hosted.flavor = "self_hosted"
    self_hosted.site_url = "https://blog.example.com"
    self_hosted.api_base = self_hosted._default_api_base()
    assert (
        self_hosted.endpoint() == "https://blog.example.com/wp-json/wp/v2/posts"
    ), self_hosted.endpoint()


def test_medium_payload(payload_config, article, shaped, fake_creds):
    medium = get_publisher("medium", payload_config, dry_run=True)
    md = medium.build_post(article, shaped["medium"])
    assert "<h1>" in md["body"]
    assert md["license"] == "all-rights-reserved"
    assert md["licenseUrl"].startswith("https://medium.com/policy")
    assert md["publishStatus"] == "public"
    assert medium.endpoint().endswith("/users/abc123/posts"), medium.endpoint()

    medium_md = get_publisher("medium", payload_config, dry_run=True)
    medium_md.content_format = "markdown"
    assert medium_md.build_post(article, shaped["medium"])["body"] == article.body_markdown


def test_webhook_payload(payload_config, article, shaped, fake_creds):
    webhook = get_publisher("webhook", payload_config, dry_run=True)
    wh = webhook.build_payload(article, shaped["webhook"])
    assert set(wh) == {"item", "platforms", "source", "markdown"}, str(set(wh))
    assert wh["item"]["canonical_url"] == CANONICAL
    assert wh["markdown"] == article.body_markdown
    assert "fake-app-password" not in str(wh) and "fake-devto-key" not in str(wh)


def test_manual_outbox_flow(payload_config, article, shaped):
    coderlegion = get_publisher("coderlegion", payload_config, dry_run=False)
    front = coderlegion._front_matter(shaped["coderlegion"], shaped["coderlegion"]["tags"])
    assert "Suggested tags:" in front
    assert "Category:" in front
    assert any(
        "content confirm" in step
        for step in coderlegion.submission_instructions(article, article.title)
    )

    with contextlib.redirect_stdout(io.StringIO()):
        manual_out = coderlegion.publish(article, shaped["coderlegion"])
    assert manual_out.ok and manual_out.mode == "manual" and not manual_out.live
    assert Path(manual_out.outbox_path).is_file(), manual_out.outbox_path
    assert article.body_markdown[:60] in Path(manual_out.outbox_path).read_text(encoding="utf-8")

    devdojo = get_publisher("devdojo", payload_config, dry_run=True)
    assert devdojo.submit_url.endswith("/community/posts/write"), devdojo.submit_url


def test_legacy_seven_still_in_registry():
    for pid in LEGACY_SEVEN:
        assert pid in PLATFORMS
    assert PLATFORMS["coderlegion"].kind == "manual"
    assert PLATFORMS["devdojo"].kind == "manual"
    assert PLATFORMS["medium"].legacy is True
