"""Phase 4 tests: roles, persistent sessions, cookie flags and log redaction."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer

import pytest

from webapp import auth
from webapp.auth import SESSION_COOKIE, SessionStore, UserStore
from webapp.jobs import redact

PASSWORD = "Correct-Horse-9!"


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


def test_redact_bearer_and_assignments():
    bearer = redact("Authorization: Bearer abc.def.ghi")
    assert "abc.def.ghi" not in bearer
    assert "Bearer ***" in bearer
    assert "supersecret" not in redact("api_key=supersecret")
    assert "supersecret" not in redact("DEVTO_API_KEY: supersecret")
    assert "supersecret" not in redact('password="supersecret"')
    assert "supersecret" not in redact("token: supersecret")


def test_redact_leaves_ordinary_text_alone():
    line = "publishing devto: attempted 3, published 2"
    assert redact(line) == line


def test_job_log_redacts():
    from webapp.jobs import Job

    job = Job(id="x", kind="publish", label="Publish")
    job.log("using DEVTO_API_KEY=abcd1234 to authenticate")
    assert "abcd1234" not in "".join(job.logs)


# --------------------------------------------------------------------------- #
# Roles on UserStore
# --------------------------------------------------------------------------- #


def test_roles_round_trip(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.add("admin@example.com", PASSWORD, confirmation=PASSWORD)
    store.add("viewer@example.com", PASSWORD, confirmation=PASSWORD, role="viewer")
    assert store.describe("admin@example.com")["role"] == "admin"
    assert store.role_of("viewer@example.com") == "viewer"

    reopened = UserStore(tmp_path / "users.json")
    assert reopened.role_of("viewer@example.com") == "viewer"

    reopened.set_role("viewer@example.com", "operator")
    assert reopened.role_of("viewer@example.com") == "operator"


def test_role_rejects_unknown(tmp_path):
    store = UserStore(tmp_path / "users.json")
    with pytest.raises(auth.AuthError):
        store.add("x@example.com", PASSWORD, confirmation=PASSWORD, role="root")


def test_last_admin_cannot_be_demoted(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.add("only@example.com", PASSWORD, confirmation=PASSWORD)
    with pytest.raises(auth.AuthError):
        store.set_role("only@example.com", "viewer")


def test_role_level_helpers():
    assert auth.role_at_least("admin", "operator")
    assert auth.role_at_least("operator", "viewer")
    assert not auth.role_at_least("viewer", "operator")
    assert auth.normalize_role("nonsense") == "viewer"
    assert auth.role_level("admin") > auth.role_level("operator") > auth.role_level("viewer")


# --------------------------------------------------------------------------- #
# Persistent sessions
# --------------------------------------------------------------------------- #


def test_persistent_session_round_trip(tmp_path):
    path = tmp_path / "sessions.json"
    first = SessionStore(path=path, master="master-key")
    token, _ = first.create("a@example.com", role="operator")
    assert path.is_file()

    second = SessionStore(path=path, master="master-key")
    session = second.get(token)
    assert session is not None
    assert session["email"] == "a@example.com"
    assert session["role"] == "operator"

    second.destroy(token)
    assert first.get(token) is not None  # in-memory copy is untouched
    assert SessionStore(path=path, master="master-key").get(token) is None


def test_persistent_session_tamper_rejected(tmp_path):
    path = tmp_path / "sessions.json"
    store = SessionStore(path=path, master="master-key")
    token, _ = store.create("a@example.com", role="admin")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["body"] = payload["body"].replace("admin", "admin ")  # invalidate the MAC
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert SessionStore(path=path, master="master-key").get(token) is None


def test_non_persistent_store_writes_nothing(tmp_path):
    store = SessionStore(master="master-key")
    store.create("a@example.com", role="admin")
    assert not (tmp_path / "sessions.json").exists()


# --------------------------------------------------------------------------- #
# HTTP: role gate, grants and cookie flags
# --------------------------------------------------------------------------- #


class Client:
    def __init__(self, base: str, token: str, host: str = "127.0.0.1") -> None:
        self.base = base
        self.token = token
        self.host = host
        self.cookie = ""

    def call(self, method, path, body=None, *, signed_in=True, host_override=""):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Host", host_override or self.host)
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
        for header in cookies:
            jar = SimpleCookie()
            jar.load(header)
            for morsel in jar.values():
                if morsel.key == SESSION_COOKIE:
                    self.cookie = f"{morsel.key}={morsel.value}"
        try:
            return status, json.loads(raw or "{}")
        except ValueError:
            return status, {"raw": raw[:200]}

    def login(self, email, password=PASSWORD):
        return self.call("POST", "/api/auth/login", {"email": email, "password": password}, signed_in=False)

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body, **kw)


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    from webapp.server import App, make_handler

    tmp = tmp_path_factory.mktemp("psb-sec")
    app = App(tmp, host="127.0.0.1", port=0)
    app.profiles.ensure_default()
    app.auth.add("owner@example.com", PASSWORD, confirmation=PASSWORD)
    app.auth.add("viewer@example.com", PASSWORD, confirmation=PASSWORD, role="viewer")
    app.auth.add("op@example.com", PASSWORD, confirmation=PASSWORD, role="operator")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.3)
    base = f"http://127.0.0.1:{port}"
    yield {"app": app, "base": base, "tmp": tmp}
    httpd.shutdown()
    httpd.server_close()


def _signed_in(server, email):
    c = Client(server["base"], server["app"].web_token)
    status, body = c.login(email)
    assert status == 200 and c.cookie, f"{status} {body}"
    return c


def _pid(server):
    return server["app"].profiles.ids()[0]


def test_viewer_can_read_but_not_write(server):
    viewer = _signed_in(server, "viewer@example.com")
    pid = _pid(server)
    assert viewer.get("/api/bootstrap")[0] == 200
    assert viewer.get(f"/api/profiles/{pid}/status")[0] == 200
    status, body = viewer.post(f"/api/profiles/{pid}/mark", {"name": "x", "status": "contacted"})
    assert status == 403, f"{status} {body}"
    assert body.get("required_role") == "operator"


def test_operator_can_write_but_not_manage_credentials(server):
    op = _signed_in(server, "op@example.com")
    pid = _pid(server)
    status, _ = op.post(f"/api/profiles/{pid}/content/remove", {"id": "does-not-exist"})
    assert status != 403, status
    status, body = op.post(f"/api/profiles/{pid}/secrets", {"smtp_password": "x"})
    assert status == 403, f"{status} {body}"
    assert body.get("required_role") == "admin"


def test_admin_can_manage_credentials(server):
    admin = _signed_in(server, "owner@example.com")
    pid = _pid(server)
    status, _ = admin.post(f"/api/profiles/{pid}/secrets", {})
    assert status != 403, status


def test_self_service_allowed_for_viewer(server):
    viewer = _signed_in(server, "viewer@example.com")
    status, _ = viewer.post("/api/auth/password", {"current": "wrong", "next": "x", "confirm": "x"})
    assert status != 403, status


def test_profile_grant_restricts_non_admins(server):
    app = server["app"]
    pid = _pid(server)
    raw = app.profiles.load_raw(pid)
    raw["security"] = {"allowed_users": ["op@example.com"]}
    app.profiles.write(pid, raw)
    try:
        viewer = _signed_in(server, "viewer@example.com")
        assert viewer.get(f"/api/profiles/{pid}/status")[0] == 403
        op = _signed_in(server, "op@example.com")
        assert op.get(f"/api/profiles/{pid}/status")[0] == 200
        admin = _signed_in(server, "owner@example.com")
        assert admin.get(f"/api/profiles/{pid}/status")[0] == 200
    finally:
        raw["security"] = {"allowed_users": []}
        app.profiles.write(pid, raw)


def test_auth_state_reports_role(server):
    admin = _signed_in(server, "owner@example.com")
    _, body = admin.get("/api/auth/state")
    assert body["role"] == "admin"
    assert body["is_admin"] is True
    assert body["can_write"] is True

    viewer = _signed_in(server, "viewer@example.com")
    _, body = viewer.get("/api/auth/state")
    assert body["role"] == "viewer"
    assert body["can_write"] is False


def test_secure_cookie_flag_on_tls_override(tmp_path_factory):
    from webapp.server import App, make_handler

    tmp = tmp_path_factory.mktemp("psb-cookie")
    app = App(tmp, host="127.0.0.1", port=0, secure_cookie="on")
    app.profiles.ensure_default()
    app.auth.add("owner@example.com", PASSWORD, confirmation=PASSWORD)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.3)
    try:
        c = Client(f"http://127.0.0.1:{port}", app.web_token)
        c.login("owner@example.com")
        assert c.cookie  # session still issued
        # With secure_cookie=on the flag is present even on plain HTTP.
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/auth/login",
            data=json.dumps({"email": "owner@example.com", "password": PASSWORD}).encode(),
            method="POST",
        )
        request.add_header("Host", "127.0.0.1")
        request.add_header("X-Web-Token", app.web_token)
        request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=20) as response:
            header = " ".join(response.headers.get_all("Set-Cookie") or [])
        assert "Secure" in header, header
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_app_persistent_sessions_writes_file(tmp_path_factory):
    from webapp.server import App

    tmp = tmp_path_factory.mktemp("psb-persist")
    app1 = App(tmp, host="127.0.0.1", port=0, persistent_sessions=True)
    app1.profiles.ensure_default()
    app1.auth.add("owner@example.com", PASSWORD, confirmation=PASSWORD)
    token, _ = app1.sessions.create("owner@example.com", role="admin")
    assert (tmp / auth.SESSION_FILENAME).is_file()

    app2 = App(tmp, host="127.0.0.1", port=0, persistent_sessions=True)
    session = app2.sessions.get(token)
    assert session is not None and session["role"] == "admin"
