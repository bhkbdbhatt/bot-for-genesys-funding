"""Web API smoke test as pytest (ported from _webtest.py).

Boots the real stdlib server on a temp base dir once per module, creates an
account, signs in, exercises the wizard's content routes, and asserts the
security boundary. Platform counts are read from the registry, so new targets
do not break these tests.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer

import pytest

from models import PLATFORM_IDS
from webapp.auth import SESSION_COOKIE


class Client:
    def __init__(self, base: str, token: str, host: str = "127.0.0.1") -> None:
        self.base = base
        self.token = token
        self.host = host
        self.cookie = ""

    def call(self, method, path, body=None, *, signed_in=True, send_token=True, host_override=""):
        url = self.base + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Host", host_override or self.host)
        if send_token:
            request.add_header("X-Web-Token", self.token)
        if signed_in and self.cookie:
            request.add_header("Cookie", self.cookie)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read().decode("utf-8")
                cookies = response.headers.get_all("Set-Cookie") or []
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            cookies = exc.headers.get_all("Set-Cookie") or []
            status = exc.code
        if cookies:
            jar = SimpleCookie()
            for header in cookies:
                jar.load(header)
            for morsel in jar.values():
                if morsel.key != SESSION_COOKIE:
                    continue
                self.cookie = f"{morsel.key}={morsel.value}"
        try:
            return status, json.loads(raw or "{}")
        except ValueError:
            return status, {"raw": raw[:200]}

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body, **kw)

    def put(self, path, body=None, **kw):
        return self.call("PUT", path, body, **kw)


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    from webapp.server import App, make_handler

    tmp = tmp_path_factory.mktemp("psb-web")
    app = App(tmp, host="127.0.0.1", port=0)
    app.profiles.ensure_default()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)
    client = Client(f"http://127.0.0.1:{port}", app.web_token)
    yield {"client": client, "app": app, "tmp": tmp, "base": f"http://127.0.0.1:{port}"}
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(scope="module")
def client(server):
    return server["client"]


@pytest.fixture(scope="module")
def signed_in_client(server):
    from webapp.server import App, make_handler  # noqa: F401

    c = Client(server["base"], server["app"].web_token)
    _, body = c.post(
        "/api/auth/setup",
        {"email": "owner@example.com", "password": "Correct-Horse-9!"},
        signed_in=False,
    )
    if not body.get("ok"):
        c.post(
            "/api/auth/login",
            {"email": "owner@example.com", "password": "Correct-Horse-9!"},
            signed_in=False,
        )
    return c


def test_security_boundary(client):
    status, _ = client.get("/api/bootstrap", signed_in=False)
    assert status == 401, status
    status, _ = client.get("/api/bootstrap", send_token=False)
    assert status == 401, status
    status, _ = client.get("/api/bootstrap", signed_in=False, host_override="evil.example.com")
    assert status == 403, status
    status, _ = client.get("/", signed_in=False)
    assert status == 200, status


def test_auth_login_and_bad_password(server, signed_in_client):
    c = Client(server["base"], server["app"].web_token)
    status, body = c.post(
        "/api/auth/login",
        {"email": "owner@example.com", "password": "Correct-Horse-9!"},
        signed_in=False,
    )
    assert status == 200, f"{status} {body}"
    assert c.cookie.startswith(SESSION_COOKIE + "="), c.cookie
    status, _ = c.post(
        "/api/auth/login",
        {"email": "owner@example.com", "password": "wrong"},
        signed_in=False,
    )
    assert status == 400, status


def test_bootstrap_payload(signed_in_client):
    status, boot = signed_in_client.get("/api/bootstrap")
    assert status == 200
    assert boot.get("channels") == ["email", "forum"]
    assert boot.get("content_statuses") == ["draft", "approved", "queued", "published", "failed"]
    assert boot.get("platform_ids") == list(PLATFORM_IDS)
    assert len(boot.get("platforms") or []) == len(PLATFORM_IDS)


def test_content_read_routes(signed_in_client):
    _, boot = signed_in_client.get("/api/bootstrap")
    pid = boot["profiles"][0]["id"]
    status, body = signed_in_client.get(f"/api/profiles/{pid}/content")
    assert status == 200, f"{status} {body}"
    assert body.get("enabled") is False
    assert body.get("enforce_gate") is True
    assert len(body.get("readiness") or []) == len(PLATFORM_IDS)
    assert body.get("items") == []

    status, body = signed_in_client.get(f"/api/profiles/{pid}/platforms")
    assert status == 200
    by_id = {entry["platform"]: entry for entry in body["readiness"]}
    assert by_id["devdojo"]["ready"] is True
    assert by_id["devto"]["ready"] is False


def test_config_edit_and_platform_validation(signed_in_client):
    _, boot = signed_in_client.get("/api/bootstrap")
    pid = boot["profiles"][0]["id"]
    _, cfg = signed_in_client.get(f"/api/profiles/{pid}")
    document = cfg["config"]
    document["content"]["enabled"] = True
    document["content"]["audience"] = "Genesys Cloud developers"
    document["content"]["angle"] = (
        "The hard part of any Genesys Cloud integration is the token lifecycle, "
        "the retry policy and the idempotency around them."
    )
    document["content"]["disclosure"] = "I maintain this project."
    document["platforms"]["devdojo"]["enabled"] = True
    document["platforms"]["coderlegion"]["enabled"] = True
    document["rate_limits"]["max_publishes_per_day"] = 10
    document["rate_limits"]["min_hours_between_platform_posts"] = 0
    status, body = signed_in_client.put(f"/api/profiles/{pid}", {"config": document})
    assert status == 200 and body.get("ok"), f"{status} {body}"

    status, body = signed_in_client.put(
        f"/api/profiles/{pid}",
        {"config": {**document, "platforms": {"nope": {"enabled": True}}}},
    )
    assert body.get("ok") is False and "nope" in str(body.get("errors"))


@pytest.fixture(scope="module")
def content_state(signed_in_client):
    """Draft, edit, approve and drive the full publish/confirm lifecycle."""
    _, boot = signed_in_client.get("/api/bootstrap")
    pid = boot["profiles"][0]["id"]
    state = {"pid": pid}

    status, body = signed_in_client.post(
        f"/api/profiles/{pid}/content/draft",
        {
            "title": "Retry policies for contact-centre APIs",
            "topic": "retry",
            "platforms": ["devdojo", "coderlegion"],
            "llm": False,
        },
    )
    assert status == 200, f"{status} {body}"
    item = body.get("item") or {}
    assert item.get("status") == "draft"
    assert item.get("platforms") == ["devdojo", "coderlegion"]
    assert all(row["publishable"] for row in body.get("gate") or []), json.dumps(body.get("gate"))
    assert (item.get("words") or 0) >= 350, str(item.get("words"))
    state["item_id"] = item["id"]

    status, body = signed_in_client.post(
        f"/api/profiles/{pid}/content/draft",
        {"title": "Thin post", "topic": "thin", "platforms": ["devdojo"], "llm": False},
    )
    assert status == 200, f"{status} {body}"
    state["thin_id"] = (body.get("item") or {}).get("id")
    return state


def test_thin_body_blocked_by_gate(signed_in_client, content_state):
    pid, thin_id = content_state["pid"], content_state["thin_id"]
    status, body = signed_in_client.put(
        f"/api/profiles/{pid}/content/items/{thin_id}",
        {"body": "# Thin post\n\nIt is short."},
    )
    gate = body.get("gate") or []
    assert any(not row["publishable"] for row in gate), json.dumps(gate)
    problems = " ".join(p for row in gate for p in row.get("problems") or [])
    assert "word minimum" in problems, problems
    assert "disclosure" in problems, problems
    assert "link to the project" in problems, problems


def test_item_detail_and_edit(signed_in_client, content_state):
    pid, item_id = content_state["pid"], content_state["item_id"]
    status, body = signed_in_client.get(f"/api/profiles/{pid}/content/items/{item_id}")
    assert status == 200 and body["item"]["id"] == item_id
    status, body = signed_in_client.put(
        f"/api/profiles/{pid}/content/items/{item_id}",
        {"body": body["item"]["body_markdown"] + "\n\nOne more line about bounded retries."},
    )
    assert status == 200, f"{status} {body}"
    assert (body["item"]["words"] or 0) >= 350
    status, _ = signed_in_client.put(f"/api/profiles/{pid}/content/items/{item_id}", {"body": "  "})
    assert status == 400


def test_approval_gate(signed_in_client, content_state):
    pid, item_id = content_state["pid"], content_state["item_id"]
    status, body = signed_in_client.get(f"/api/profiles/{pid}/content")
    approved = [row for row in body["items"] if row["id"] == item_id]
    assert len(approved) == 1
    assert all(entry["status"] == "pending" for entry in approved[0]["publications"].values())
    status, body = signed_in_client.post(f"/api/profiles/{pid}/content/approve", {"id": item_id})
    assert status == 200 and body["item"]["status"] == "approved", f"{status} {body}"
    assert body.get("pending_platforms") == ["devdojo", "coderlegion"], str(body.get("pending_platforms"))
    status, _ = signed_in_client.post(f"/api/profiles/{pid}/content/approve", {"id": "does-not-exist"})
    assert status == 404


def _run_job(client, pid, body):
    status, resp = client.post(f"/api/profiles/{pid}/content/publish", body)
    assert status == 200 and resp.get("job"), f"{status} {resp}"
    job_id = resp["job"]["id"]
    job = None
    for _ in range(150):
        status, resp = client.get(f"/api/jobs/{job_id}?since=0")
        job = resp["job"]
        if job["state"] in {"done", "error", "cancelled"}:
            break
        time.sleep(0.1)
    return job


def test_publish_dry_run(signed_in_client, content_state):
    pid, item_id = content_state["pid"], content_state["item_id"]
    job = _run_job(
        signed_in_client, pid, {"ids": [item_id], "dry_run": True}
    )
    assert job["state"] == "done", job.get("error") or job["state"]
    result = (job.get("result") or {}).get("result") or {}
    assert result.get("attempted") == 2, json.dumps(result)[:300]
    assert result.get("published") == 0
    assert any("dry run" in line.lower() for line in job.get("logs") or [])

    _, body = signed_in_client.get(f"/api/profiles/{pid}/content")
    assert [row for row in body["items"] if row["id"] == item_id][0]["status"] == "approved"
    assert body["summary"]["daily_usage"]["devdojo"]["used"] == 0


def test_publish_live_manual_targets(signed_in_client, content_state):
    pid, item_id = content_state["pid"], content_state["item_id"]
    job = _run_job(
        signed_in_client, pid, {"ids": [item_id], "platforms": ["devdojo", "coderlegion"], "dry_run": False}
    )
    assert job["state"] == "done", job.get("error") or job["state"]
    result = (job.get("result") or {}).get("result") or {}
    assert result.get("queued") == 2, json.dumps(result)[:400]
    assert "content confirm" in (job.get("result") or {}).get("hint", "")


def test_outbox_written(signed_in_client, content_state, server):
    pid, item_id = content_state["pid"], content_state["item_id"]
    outbox = server["tmp"] / "profiles" / pid / "content_outbox"
    assert (outbox / "devdojo" / f"{item_id}.md").is_file()
    assert (outbox / "coderlegion" / f"{item_id}.md").is_file()


def test_confirm_and_reset(signed_in_client, content_state):
    pid, item_id = content_state["pid"], content_state["item_id"]
    _, body = signed_in_client.get(f"/api/profiles/{pid}/content")
    stored = [row for row in body["items"] if row["id"] == item_id][0]
    assert stored["status"] == "queued", stored["status"]
    assert all(not p.get("url") for p in stored["publications"].values())

    status, _ = signed_in_client.post(
        f"/api/profiles/{pid}/content/confirm",
        {"id": item_id, "platform": "devdojo", "url": "not-a-url"},
    )
    assert status == 400
    status, _ = signed_in_client.post(
        f"/api/profiles/{pid}/content/confirm", {"id": item_id, "platform": "devdojo"}
    )
    assert status == 400

    status, body = signed_in_client.post(
        f"/api/profiles/{pid}/content/confirm",
        {"id": item_id, "platform": "devdojo", "url": "https://devdojo.com/community/posts/x"},
    )
    assert status == 200, f"{status} {body}"
    assert body["item"]["status"] == "queued"
    status, body = signed_in_client.post(
        f"/api/profiles/{pid}/content/confirm",
        {"id": item_id, "platform": "coderlegion", "url": "https://coderlegion.com/1/x"},
    )
    assert body["item"]["status"] == "published", body["item"]["status"]
    assert len(body.get("live_urls") or []) == 2


def test_reset(signed_in_client, content_state):
    pid, item_id = content_state["pid"], content_state["item_id"]
    status, body = signed_in_client.post(
        f"/api/profiles/{pid}/content/reset", {"id": item_id, "platform": "devdojo"}
    )
    assert status == 200, f"{status} {body}"
    assert body["item"]["status"] == "approved"
    assert not body["item"]["publications"]["devdojo"].get("url")
    status, _ = signed_in_client.post(
        f"/api/profiles/{pid}/content/reset", {"id": item_id, "platform": "nope"}
    )
    assert status == 400


def test_secrets_go_to_env_not_disk(signed_in_client, content_state, server, monkeypatch):
    import os

    pid = content_state["pid"]
    monkeypatch.delenv("DEVTO_API_KEY", raising=False)
    status, body = signed_in_client.post(
        f"/api/profiles/{pid}/content/secrets",
        {"values": {"devto": "fake-key-for-testing", "devdojo": "should-be-refused"}},
    )
    assert "DEVTO_API_KEY" in json.dumps(body.get("applied")), json.dumps(body)
    assert os.environ.get("DEVTO_API_KEY") == "fake-key-for-testing"
    assert "devdojo" in (body.get("problems") or {}), json.dumps(body)
    on_disk = (server["tmp"] / "profiles" / pid / "config.yaml").read_text(encoding="utf-8")
    assert "fake-key-for-testing" not in on_disk
    os.environ.pop("DEVTO_API_KEY", None)


def test_cleanup_routes(signed_in_client, content_state):
    pid, item_id, thin_id = content_state["pid"], content_state["item_id"], content_state["thin_id"]
    status, body = signed_in_client.post(f"/api/profiles/{pid}/content/remove", {"id": thin_id})
    assert status == 200 and body.get("removed") == thin_id, f"{status} {body}"
    status, _ = signed_in_client.post(f"/api/profiles/{pid}/content/remove", {"id": thin_id})
    assert status == 404
    status, _ = signed_in_client.post(
        f"/api/profiles/{pid}/content/mark", {"id": item_id, "status": "not-a-status"}
    )
    assert status == 400


def test_logout_destroys_session(server):
    c = Client(server["base"], server["app"].web_token)
    c.post(
        "/api/auth/login",
        {"email": "owner@example.com", "password": "Correct-Horse-9!"},
        signed_in=False,
    )
    assert c.cookie
    status, _ = c.post("/api/auth/logout", {})
    assert status == 200
    c.cookie = ""
    status, _ = c.get("/api/bootstrap")
    assert status == 401, status
