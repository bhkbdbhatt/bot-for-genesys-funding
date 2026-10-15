"""Webhook signing (HMAC) and the shared bounded-retry policy."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Dict, List, Optional

import pytest

from models import ContentItem
from publishers import get_publisher

SECRET = "topsecret-signing-key"
URL = "https://hooks.example.com/ingest"


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _config(config_factory, *, retry: Optional[Dict[str, Any]] = None, **webhook: Any):
    """Example-based config with only the webhook platform enabled."""
    base = {"enabled": True, "url": URL}

    def mutate(document: Dict[str, Any]) -> None:
        document.setdefault("platforms", {})["webhook"] = dict(base, **webhook)
        for pid in list(document["platforms"]):
            if pid != "webhook":
                document["platforms"][pid]["enabled"] = False
        if retry is not None:
            document.setdefault("publishing", {})["retry"] = dict(
                {"attempts": 3, "backoff_seconds": 2}, **retry
            )

    return config_factory("webhook", mutate=mutate)


def _article() -> ContentItem:
    item = ContentItem(
        id="wh-post",
        title="A signed webhook",
        body_markdown="Body text about Genesys Cloud.\n\nhttps://github.com/example/demo",
        summary="Signed delivery.",
        platforms=["webhook"],
        tags=["genesys"],
        canonical_url="https://blog.example.com/signed",
    )
    item.fingerprint = item.compute_fingerprint()
    return item


def _payload(item: ContentItem) -> Dict[str, Any]:
    return {
        "title": item.title,
        "body": item.body_markdown,
        "summary": item.summary,
        "tags": list(item.tags),
        "canonical_url": item.canonical_url,
        "words": len(item.body_markdown.split()),
    }


class _Response:
    def __init__(self, status: int, *, payload: Optional[Dict] = None,
                 headers: Optional[Dict[str, str]] = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = text

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


# --------------------------------------------------------------------------- #
# Signing
# --------------------------------------------------------------------------- #


def test_signature_matches_reference_hmac(config_factory, monkeypatch, clean_env):
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr("publishers.webhook.time.time", lambda: 1_700_000_000.0)
    publisher = get_publisher("webhook", _config(config_factory), dry_run=True)
    item = _article()
    raw = publisher.serialize(publisher.build_payload(item, _payload(item)))

    headers = publisher.headers(raw, item)

    expected = hmac.new(SECRET.encode(), b"1700000000." + raw, hashlib.sha256).hexdigest()
    assert headers["X-Timestamp"] == "1700000000"
    assert headers["X-Signature"] == f"sha256={expected}"
    assert headers["X-Idempotency-Key"] == item.fingerprint


def test_serialize_is_deterministic(config_factory):
    publisher = get_publisher("webhook", _config(config_factory), dry_run=True)
    body = {"b": 2, "a": 1, "nested": {"y": [1, 2], "x": "café"}}
    first = publisher.serialize(body)
    assert first == publisher.serialize({"a": 1, "nested": {"x": "café", "y": [1, 2]}, "b": 2})
    assert b", " not in first  # compact separators


def test_auto_signing_off_without_secret(config_factory, clean_env):
    publisher = get_publisher("webhook", _config(config_factory), dry_run=True)
    assert publisher.should_sign is False
    headers = publisher.headers(b"{}", _article())
    assert "X-Signature" not in headers
    assert "X-Timestamp" not in headers


def test_signing_on_requires_secret(config_factory, clean_env):
    publisher = get_publisher("webhook", _config(config_factory, signing="on"), dry_run=True)
    ok, why = publisher.check_ready()
    assert ok is False
    assert "WEBHOOK_SECRET" in why


def test_signing_off_never_signs_even_with_secret(config_factory, monkeypatch, clean_env):
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    publisher = get_publisher("webhook", _config(config_factory, signing="off"), dry_run=True)
    assert publisher.should_sign is False
    assert "X-Signature" not in publisher.headers(b"{}", _article())


def test_invalid_signing_mode_fails_ready(config_factory):
    publisher = get_publisher("webhook", _config(config_factory, signing="maybe"), dry_run=True)
    ok, why = publisher.check_ready()
    assert ok is False
    assert "signing" in why


def test_idempotency_key_falls_back_to_computed_fingerprint(config_factory, clean_env):
    publisher = get_publisher("webhook", _config(config_factory), dry_run=True)
    item = _article()
    item.fingerprint = ""
    headers = publisher.headers(b"{}", item)
    assert headers["X-Idempotency-Key"] == item.compute_fingerprint()


def test_idempotency_key_absent_without_item(config_factory, clean_env):
    publisher = get_publisher("webhook", _config(config_factory), dry_run=True)
    assert "X-Idempotency-Key" not in publisher.headers(b"{}")


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #


def test_deliver_posts_signed_raw_body(config_factory, monkeypatch, clean_env):
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    publisher = get_publisher("webhook", _config(config_factory), dry_run=False)
    item = _article()
    captured: Dict[str, Any] = {}

    def fake_request(method, url, *, data=None, headers=None, **_kwargs):
        captured.update(method=method, url=url, data=data, headers=headers)
        return _Response(200, payload={"url": "https://hooks.example.com/p/1"})

    publisher._request = fake_request  # type: ignore[method-assign]
    outcome = publisher.publish(item, _payload(item))

    assert outcome.ok and outcome.live
    assert captured["method"] == "POST" and captured["url"] == URL
    assert isinstance(captured["data"], bytes)
    assert "X-Signature" in captured["headers"]
    assert captured["headers"]["X-Idempotency-Key"] == item.fingerprint


def test_dry_run_never_transmits(config_factory, clean_env):
    publisher = get_publisher("webhook", _config(config_factory), dry_run=True)

    def tripwire(*_a, **_k):
        raise AssertionError("dry-run must not transmit")

    publisher._request = tripwire  # type: ignore[method-assign]
    assert publisher.publish(_article(), _payload(_article())).ok


# --------------------------------------------------------------------------- #
# Retry policy
# --------------------------------------------------------------------------- #


def _stub(monkeypatch, responses: List[_Response]) -> Dict[str, List]:
    """Feed `responses` to requests.request and record calls + sleeps."""
    log: Dict[str, List] = {"calls": [], "sleeps": []}
    queue = list(responses)

    def fake_request(method, url, **_kwargs):
        log["calls"].append(url)
        return queue.pop(0)

    monkeypatch.setattr("publishers.base.requests.request", fake_request)
    monkeypatch.setattr("publishers.base.time.sleep", lambda d: log["sleeps"].append(d))
    return log


def test_retries_5xx_then_succeeds(config_factory, monkeypatch):
    publisher = get_publisher("webhook", _config(config_factory), dry_run=True)
    log = _stub(monkeypatch, [_Response(503), _Response(500), _Response(200, payload={})])

    response = publisher._request("POST", URL, data=b"{}")

    assert response.status_code == 200
    assert len(log["calls"]) == 3
    assert log["sleeps"] == [2.0, 4.0]  # exponential


def test_retries_exhausted_raises(config_factory, monkeypatch):
    from publishers.base import PublishError

    publisher = get_publisher("webhook", _config(config_factory), dry_run=True)
    _stub(monkeypatch, [_Response(500), _Response(502), _Response(503)])

    with pytest.raises(PublishError, match="HTTP 503"):
        publisher._request("POST", URL, data=b"{}")


def test_client_error_is_terminal(config_factory, monkeypatch):
    from publishers.base import PublishError

    publisher = get_publisher("webhook", _config(config_factory), dry_run=True)
    log = _stub(monkeypatch, [_Response(400, text="bad request"), _Response(200)])

    with pytest.raises(PublishError, match="HTTP 400"):
        publisher._request("POST", URL, data=b"{}")
    assert len(log["calls"]) == 1  # never retried
    assert log["sleeps"] == []


def test_rate_limit_honours_retry_after(config_factory, monkeypatch):
    publisher = get_publisher("webhook", _config(config_factory), dry_run=True)
    log = _stub(
        monkeypatch,
        [_Response(429, headers={"Retry-After": "7"}), _Response(200, payload={})],
    )

    publisher._request("POST", URL, data=b"{}")

    assert log["sleeps"] == [7.0]  # server floor beats the 2s base


def test_network_error_retried_then_succeeds(config_factory, monkeypatch):
    import requests

    publisher = get_publisher("webhook", _config(config_factory), dry_run=True)
    calls = {"n": 0}
    sleeps: List[float] = []

    def flaky(method, url, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.ConnectionError("reset by peer")
        return _Response(200, payload={})

    monkeypatch.setattr("publishers.base.requests.request", flaky)
    monkeypatch.setattr("publishers.base.time.sleep", lambda d: sleeps.append(d))

    assert publisher._request("POST", URL, data=b"{}").status_code == 200
    assert calls["n"] == 2 and sleeps == [2.0]


def test_network_error_exhausted_raises(config_factory, monkeypatch):
    import requests

    from publishers.base import PublishError

    publisher = get_publisher("webhook", _config(config_factory), dry_run=True)

    def boom(*_a, **_k):
        raise requests.Timeout("slow")

    monkeypatch.setattr("publishers.base.requests.request", boom)
    monkeypatch.setattr("publishers.base.time.sleep", lambda _d: None)

    with pytest.raises(PublishError, match="timed out"):
        publisher._request("POST", URL, data=b"{}")


def test_attempts_config_is_clamped(config_factory):
    publisher = get_publisher("webhook", _config(config_factory, retry={"attempts": 99}), dry_run=True)
    assert publisher._retry_attempts() == 10


def test_backoff_base_config(config_factory):
    publisher = get_publisher(
        "webhook", _config(config_factory, retry={"backoff_seconds": 0.5}), dry_run=True
    )
    assert publisher._backoff_delay(publisher._retry_backoff(), 1) == 0.5
    assert publisher._backoff_delay(publisher._retry_backoff(), 2) == 1.0
