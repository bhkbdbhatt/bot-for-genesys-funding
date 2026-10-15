"""Ghost, Bluesky and Substack: specs, payloads, signing and hand-off."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from config_loader import Config
from models import PLATFORM_IDS, ContentItem
from platforms import PLATFORMS
from publishers import get_publisher

NEW_IDS = ("ghost", "bluesky", "substack")

GHOST_KEY = "65d99cd9447d15d98a2fc264:deadbeefcafe"
GHOST_ADMIN_URL = "https://blog.example.com/ghost"


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_registry_contains_new_targets():
    for pid in NEW_IDS:
        assert pid in PLATFORM_IDS
        assert pid in PLATFORMS
    assert PLATFORMS["ghost"].kind == "api"
    assert PLATFORMS["ghost"].content_format == "html"
    assert PLATFORMS["ghost"].token_env == "GHOST_ADMIN_KEY"
    assert PLATFORMS["bluesky"].kind == "api"
    assert PLATFORMS["bluesky"].supports_tags is False
    assert PLATFORMS["bluesky"].token_env == "BLUESKY_APP_PASSWORD"
    assert PLATFORMS["substack"].kind == "manual"
    assert PLATFORMS["substack"].submission_url.startswith("https://")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


@pytest.fixture()
def new_platform_config(tmp_path, repo_root):
    doc = yaml.safe_load((repo_root / "config.example.yaml").read_text(encoding="utf-8"))
    doc.setdefault("paths", {})
    doc["paths"]["content_file"] = "newp-content.json"
    doc["paths"]["tracker_file"] = "newp-sponsors.json"
    doc.setdefault("rate_limits", {})
    doc["rate_limits"]["max_publishes_per_day"] = 20
    doc["rate_limits"]["min_hours_between_platform_posts"] = 0
    for pid in NEW_IDS:
        doc.setdefault("platforms", {}).setdefault(pid, {})["enabled"] = True
    target = tmp_path / "config.yaml"
    target.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return Config.load(target, create_from_example=False)


@pytest.fixture()
def article():
    item = ContentItem(
        id="new-platforms-post",
        title="Stop refreshing tokens by hand",
        body_markdown=(
            "# Stop refreshing tokens by hand\n\n## Why it hurts\n\n"
            + ("A sentence about token lifecycle. " * 40)
            + "\n\nI maintain this project.\n\nhttps://github.com/purecloudlabs/community-plugin-example"
        ),
        summary="A practical token lifecycle for Genesys Cloud integrations.",
        platforms=list(NEW_IDS),
        tags=["genesys", "opentelemetry", "opensource"],
        canonical_url="https://blog.example.com/posts/stop-refreshing-tokens",
    )
    item.fingerprint = item.compute_fingerprint()
    return item


def _payload(article: ContentItem) -> Dict[str, Any]:
    return {
        "title": article.title,
        "body": article.body_markdown,
        "summary": article.summary,
        "tags": list(article.tags),
        "canonical_url": article.canonical_url,
        "words": len(article.body_markdown.split()),
        "content_format": "markdown",
    }


# --------------------------------------------------------------------------- #
# Ghost
# --------------------------------------------------------------------------- #


def test_ghost_jwt_is_valid_hs256():
    from publishers.ghost import JWT_TTL_SECONDS, sign_admin_jwt

    jwt = sign_admin_jwt(GHOST_KEY, now=1_700_000_000)
    header_b64, claims_b64, signature_b64 = jwt.split(".")

    def decode(part: str) -> Dict[str, Any]:
        padded = part + "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))

    header = decode(header_b64)
    claims = decode(claims_b64)
    assert header == {"typ": "JWT", "alg": "HS256"}
    assert claims["iat"] == 1_700_000_000
    assert claims["exp"] == 1_700_000_000 + JWT_TTL_SECONDS
    assert claims["aud"] == "/admin/api/posts/"

    import hashlib
    import hmac

    secret = bytes.fromhex("deadbeefcafe")
    expected = base64.urlsafe_b64encode(
        hmac.new(secret, f"{header_b64}.{claims_b64}".encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")
    assert signature_b64 == expected


def test_ghost_jwt_rejects_bad_key():
    from publishers.base import PublishError
    from publishers.ghost import sign_admin_jwt

    with pytest.raises(PublishError):
        sign_admin_jwt("not-a-jwt-key")
    with pytest.raises(PublishError):
        sign_admin_jwt("id:not-hex-secret!!")


def test_ghost_endpoint_normalises_ghost_suffix(tmp_path, repo_root, monkeypatch):
    monkeypatch.setenv("GHOST_ADMIN_KEY", GHOST_KEY)
    monkeypatch.setenv("GHOST_ADMIN_URL", GHOST_ADMIN_URL)
    doc = yaml.safe_load((repo_root / "config.example.yaml").read_text(encoding="utf-8"))
    doc["platforms"]["ghost"]["admin_url"] = "https://blog.example.com"
    doc.setdefault("paths", {})
    doc["paths"]["content_file"] = "ep-content.json"
    doc["paths"]["tracker_file"] = "ep-sponsors.json"
    target = tmp_path / "config.yaml"
    target.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    config = Config.load(target, create_from_example=False)

    publisher = get_publisher("ghost", config, dry_run=True)
    assert publisher.endpoint == "https://blog.example.com/ghost/api/v5.0/admin/posts/"

    doc["platforms"]["ghost"]["admin_url"] = GHOST_ADMIN_URL
    target.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    config2 = Config.load(target, create_from_example=False)
    publisher2 = get_publisher("ghost", config2, dry_run=True)
    assert publisher2.endpoint == f"{GHOST_ADMIN_URL}/api/admin/posts/"


def test_ghost_readiness(new_platform_config, monkeypatch):
    monkeypatch.delenv("GHOST_ADMIN_KEY", raising=False)
    monkeypatch.delenv("GHOST_ADMIN_URL", raising=False)
    publisher = get_publisher("ghost", new_platform_config, dry_run=True)
    ready, reason = publisher.check_ready()
    assert not ready and "GHOST_ADMIN_KEY" in reason, reason

    monkeypatch.setenv("GHOST_ADMIN_KEY", "no-colon-key")
    monkeypatch.setenv("GHOST_ADMIN_URL", GHOST_ADMIN_URL)
    ready, reason = publisher.check_ready()
    assert not ready and "Admin API key" in reason, reason

    monkeypatch.setenv("GHOST_ADMIN_KEY", GHOST_KEY)
    monkeypatch.delenv("GHOST_ADMIN_URL", raising=False)
    ready, reason = publisher.check_ready()
    assert not ready and "GHOST_ADMIN_URL" in reason, reason

    monkeypatch.setenv("GHOST_ADMIN_URL", "http://insecure.example.com")
    ready, reason = publisher.check_ready()
    assert not ready and "https" in reason, reason

    monkeypatch.setenv("GHOST_ADMIN_URL", GHOST_ADMIN_URL)
    ready, reason = publisher.check_ready()
    assert ready, reason


def test_ghost_payload_converts_markdown_to_html(new_platform_config, monkeypatch):
    monkeypatch.setenv("GHOST_ADMIN_KEY", GHOST_KEY)
    monkeypatch.setenv("GHOST_ADMIN_URL", GHOST_ADMIN_URL)
    publisher = get_publisher("ghost", new_platform_config, dry_run=True)
    article_item = _item_for_ghost()
    post = publisher.build_post(article_item, _payload(article_item))
    assert post["status"] == "published"
    assert post["html"].startswith("<h1>")
    assert "\n## " not in post["html"]
    assert post["tags"] == [{"name": "genesys"}]
    assert post["canonical_url"] == "https://blog.example.com/x"
    assert post["custom_excerpt"] == "Short summary."


def _item_for_ghost() -> ContentItem:
    item = ContentItem(
        id="ghost-item",
        title="Stop refreshing tokens by hand",
        body_markdown="# Head\n\nSome **body** text.",
        summary="Short summary.",
        platforms=["ghost"],
        tags=["genesys"],
        canonical_url="https://blog.example.com/x",
    )
    item.fingerprint = item.compute_fingerprint()
    return item


def test_ghost_dry_run_opens_no_socket(new_platform_config, monkeypatch):
    monkeypatch.setenv("GHOST_ADMIN_KEY", GHOST_KEY)
    monkeypatch.setenv("GHOST_ADMIN_URL", GHOST_ADMIN_URL)
    publisher = get_publisher("ghost", new_platform_config, dry_run=True)
    opened: list[str] = []

    def tripwire(*_a, **_k):
        opened.append("net")
        raise AssertionError("network during dry run")

    publisher._request = tripwire  # type: ignore[method-assign]
    item = _item_for_ghost()
    outcome = publisher.publish(item, _payload(item))
    assert outcome.dry_run is True
    assert not opened


# --------------------------------------------------------------------------- #
# Bluesky
# --------------------------------------------------------------------------- #


def test_bluesky_plain_text_strips_markdown_and_urls():
    from publishers.bluesky import plain_text

    text = plain_text(
        "# Title line\n\nSome **bold** and *italic* with a [link](https://x.example/p) and https://bare.example/z."
    )
    assert "#" not in text
    assert "**" not in text
    assert "https://" not in text
    assert "Title line" in text
    assert "bold" in text and "link" in text


def test_bluesky_plain_text_clips_to_limit():
    from publishers.bluesky import TEXT_LIMIT, plain_text

    text = plain_text("word " * 200)
    assert len(text) <= TEXT_LIMIT


def test_bluesky_record_and_at_uri_conversion(new_platform_config, monkeypatch):
    monkeypatch.setenv("BLUESKY_HANDLE", "maintainer.bsky.social")
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "app-pass-fake")
    publisher = get_publisher("bluesky", new_platform_config, dry_run=True)
    item = _item_for_bluesky()
    record = publisher.build_record(item, _payload(item), did="did:plc:abc123")

    assert record["$type"] == "app.bsky.feed.post"
    assert len(record["text"]) <= 300
    assert "https://blog.example.com/posts/stop-refreshing-tokens" in record["text"]
    embed = record["embed"]
    assert embed["$type"] == "app.bsky.embed.external"
    assert embed["external"]["uri"] == item.canonical_url
    assert embed["external"]["title"] == item.title

    from publishers.bluesky import _at_uri_to_web

    web = _at_uri_to_web("at://did:plc:abc123/app.bsky.feed.post/3kxyz")
    assert web == "https://bsky.app/profile/did:plc:abc123/post/3kxyz"
    assert _at_uri_to_web("nonsense") == ""


def _item_for_bluesky() -> ContentItem:
    item = ContentItem(
        id="bluesky-item",
        title="Stop refreshing tokens by hand",
        body_markdown="# Head\n\nBody text.",
        summary="A practical token lifecycle for Genesys Cloud integrations.",
        platforms=["bluesky"],
        tags=[],
        canonical_url="https://blog.example.com/posts/stop-refreshing-tokens",
    )
    item.fingerprint = item.compute_fingerprint()
    return item


def test_bluesky_readiness(new_platform_config, monkeypatch):
    monkeypatch.delenv("BLUESKY_HANDLE", raising=False)
    monkeypatch.delenv("BLUESKY_APP_PASSWORD", raising=False)
    publisher = get_publisher("bluesky", new_platform_config, dry_run=True)
    ready, reason = publisher.check_ready()
    assert not ready and "BLUESKY_HANDLE" in reason, reason

    monkeypatch.setenv("BLUESKY_HANDLE", "maintainer.bsky.social")
    ready, reason = publisher.check_ready()
    assert not ready and "BLUESKY_APP_PASSWORD" in reason, reason

    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "app-pass-fake")
    ready, reason = publisher.check_ready()
    assert ready, reason


def test_bluesky_deliver_flows_through_stubbed_http(new_platform_config, monkeypatch):
    """Session + createRecord: stub _request, assert the outcome URL rewrite."""
    monkeypatch.setenv("BLUESKY_HANDLE", "maintainer.bsky.social")
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "app-pass-fake")
    publisher = get_publisher("bluesky", new_platform_config, dry_run=False)

    calls: list[Dict[str, Any]] = []

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload
            self.status_code = 200
            self.headers = {}

        def json(self):
            return self._payload

    def fake_request(method, url, *, json_body=None, headers=None, **_k):
        calls.append({"method": method, "url": url, "json": json_body, "headers": headers})
        if url.endswith("createSession"):
            return FakeResponse(
                {"accessJwt": "fake-access", "did": "did:plc:abc123", "handle": "maintainer.bsky.social"}
            )
        return FakeResponse({"uri": "at://did:plc:abc123/app.bsky.feed.post/3kxyz", "cid": "bafy"})

    publisher._request = fake_request  # type: ignore[method-assign]
    item = _item_for_bluesky()
    outcome = publisher.publish(item, _payload(item))
    assert outcome.ok and outcome.live
    assert outcome.url == "https://bsky.app/profile/did:plc:abc123/post/3kxyz"
    assert outcome.external_id == "at://did:plc:abc123/app.bsky.feed.post/3kxyz"
    assert len(calls) == 2
    assert calls[0]["url"].endswith("createSession")
    assert calls[0]["json"]["identifier"] == "maintainer.bsky.social"
    assert calls[1]["json"]["repo"] == "did:plc:abc123"
    assert calls[1]["json"]["collection"] == "app.bsky.feed.post"
    assert calls[1]["headers"]["Authorization"] == "Bearer fake-access"
    # The app password is only ever in the createSession body (the protocol's
    # own shape); it must never appear in the record call or its headers.
    assert "app-pass-fake" not in json.dumps(calls[1])


def test_bluesky_dry_run_opens_no_socket(new_platform_config, monkeypatch):
    monkeypatch.setenv("BLUESKY_HANDLE", "maintainer.bsky.social")
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "app-pass-fake")
    publisher = get_publisher("bluesky", new_platform_config, dry_run=True)
    opened: list[str] = []

    def tripwire(*_a, **_k):
        opened.append("net")
        raise AssertionError("network during dry run")

    publisher._request = tripwire  # type: ignore[method-assign]
    item = _item_for_bluesky()
    outcome = publisher.publish(item, _payload(item))
    assert outcome.dry_run is True
    assert not opened


# --------------------------------------------------------------------------- #
# Substack
# --------------------------------------------------------------------------- #


def test_substack_manual_hand_off(new_platform_config, tmp_path):
    publisher = get_publisher("substack", new_platform_config, dry_run=False)
    assert publisher.submit_url.startswith("https://")
    ready, reason = publisher.check_ready()
    assert ready, reason

    item = ContentItem(
        id="substack-item",
        title="Stop refreshing tokens by hand",
        body_markdown="# Head\n\nBody with the project link https://github.com/purecloudlabs/community-plugin-example",
        summary="s",
        platforms=["substack"],
        tags=["genesys"],
    )
    item.fingerprint = item.compute_fingerprint()
    payload = _payload(item)
    payload["body"] = item.body_markdown
    outcome = publisher.publish(item, payload)
    assert outcome.ok and outcome.mode == "manual" and not outcome.live
    assert outcome.outbox_path
    outbox = Path(outcome.outbox_path)
    assert outbox.is_file()
    assert item.body_markdown[:40] in outbox.read_text(encoding="utf-8")
    assert any("content confirm" in step for step in outcome.instructions)
