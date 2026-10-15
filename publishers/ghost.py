"""Ghost publisher - Admin API with a per-request HS256 JWT.

Ghost's Admin API is REST but authentication is a signed JWT rather than a
static bearer token. The Admin API key is ``<id>:<secret>`` (hex secret); the
publisher signs a short-lived (5 minute) HS256 token per request with stdlib
``hmac``/``hashlib``/``base64`` - no JWT dependency, no clock-skew surprises
from a pre-minted token sitting in a log.

Endpoints (the site's ``/ghost`` base is ``platforms.ghost.admin_url`` or the
``GHOST_ADMIN_URL`` environment variable):

    POST <admin_url>/api/admin/posts/     {"posts": [{title, html, status, ...}]}

The response carries the created post (``id``, ``slug``, ``url``); the outcome
URL prefers Ghost's own ``url`` and falls back to ``site_url + / + slug``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict

from logging_setup import get_logger
from models import ContentItem
from publishers.base import BasePublisher, PublishError, PublishOutcome
from publishers.markdown_html import markdown_to_html

LOG = get_logger("publisher-ghost")

DEFAULT_API_VERSION = "v5.0"
JWT_TTL_SECONDS = 300
#: Path Ghost wants in the JWT `aud` claim for post creation.
DEFAULT_AUDIENCE = "/admin/api/posts/"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def sign_admin_jwt(admin_key: str, *, audience: str = DEFAULT_AUDIENCE, now: int | None = None) -> str:
    """Sign a Ghost Admin API JWT from an ``<id>:<secret>`` key.

    The secret half is hex, per Ghost's integration key format. Pure function,
    exposed for tests.
    """
    key_id, separator, secret_hex = admin_key.partition(":")
    if not separator or not key_id or not secret_hex:
        raise PublishError("GHOST_ADMIN_KEY must look like '<id>:<secret>' (Settings -> Integrations)")
    try:
        secret = bytes.fromhex(secret_hex)
    except ValueError as exc:
        raise PublishError("the secret half of GHOST_ADMIN_KEY must be hex") from exc

    timestamp = int(time.time()) if now is None else int(now)
    header = _b64url(json.dumps({"typ": "JWT", "alg": "HS256"}, separators=(",", ":")).encode())
    claims = _b64url(
        json.dumps(
            {"iat": timestamp, "exp": timestamp + JWT_TTL_SECONDS, "aud": audience},
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{claims}".encode("ascii")
    signature = _b64url(hmac.new(secret, signing_input, hashlib.sha256).digest())
    return f"{header}.{claims}.{signature}"


class GhostPublisher(BasePublisher):
    platform = "ghost"

    def __init__(self, config, *, dry_run: bool = False) -> None:
        super().__init__(config, dry_run=dry_run, platform_id=self.platform)
        self.api_version = self.opt_str("api_version", DEFAULT_API_VERSION).strip("/") or DEFAULT_API_VERSION
        self.status = (self.opt_str("status", "published") or "published").lower()
        self.site_url = self.opt_str("site_url").rstrip("/")
        self.audience = self.opt_str("audience", DEFAULT_AUDIENCE) or DEFAULT_AUDIENCE

    # -- configuration ----------------------------------------------------- #

    @property
    def admin_url(self) -> str:
        from config_loader import resolve_secret

        explicit = self.opt_str("admin_url")
        if explicit:
            return explicit.rstrip("/")
        return (resolve_secret("GHOST_ADMIN_URL") or "").rstrip("/")

    @property
    def endpoint(self) -> str:
        base = self.admin_url
        if not base:
            return ""
        if base.endswith("/ghost"):
            return f"{base}/api/admin/posts/"
        return f"{base}/ghost/api/{self.api_version}/admin/posts/"

    def check_ready(self) -> tuple[bool, str]:
        if not self.token:
            return False, f"environment variable {self.token_env} is not set"
        if ":" not in self.token:
            return False, f"{self.token_env} must be an Admin API key of the form '<id>:<secret>'"
        if not self.admin_url:
            return False, "platforms.ghost.admin_url is empty and GHOST_ADMIN_URL is not set"
        if not self.admin_url.startswith("https://"):
            return False, f"Ghost admin URL must be https (got {self.admin_url[:40]!r})"
        if self.status not in {"published", "draft"}:
            return False, f"platforms.ghost.status must be 'published' or 'draft' (got {self.status!r})"
        return True, "ok"

    def auth_headers(self) -> Dict[str, str]:
        jwt = sign_admin_jwt(self.token, audience=self.audience)
        return {"Authorization": f"Ghost {jwt}", "Content-Type": "application/json"}

    # -- payload ----------------------------------------------------------- #

    def build_post(self, item: ContentItem, payload: Dict[str, Any]) -> Dict[str, Any]:
        title = payload.get("title") or item.title
        markdown = payload.get("body") or item.body_markdown
        post: Dict[str, Any] = {
            "title": title,
            "html": markdown_to_html(markdown),
            "status": self.status,
        }
        tags = list(payload.get("tags") or item.tags)[: self.spec.tag_limit]
        if tags:
            post["tags"] = [{"name": tag} for tag in tags]
        summary = payload.get("summary") or item.summary
        if summary:
            post["custom_excerpt"] = str(summary)[:300]
        canonical = payload.get("canonical_url") or item.canonical_url
        if canonical and self.spec.supports_canonical:
            post["canonical_url"] = canonical
        return post

    # -- delivery ---------------------------------------------------------- #

    def _deliver(self, item: ContentItem, payload: Dict[str, Any]) -> PublishOutcome:
        post = self.build_post(item, payload)
        response = self._request(
            "POST",
            self.endpoint,
            json_body={"posts": [post]},
            headers=self.auth_headers(),
            label=f"ghost publish {item.id}",
        )
        body = self._json(response)
        created = (body.get("posts") or [{}])[0]
        url = str(created.get("url") or "")
        if not url and self.site_url and created.get("slug"):
            url = f"{self.site_url}/{created['slug']}"
        if self.status == "draft" or not url:
            live = False
            detail = "draft created - publish it from the Ghost admin" if self.status == "draft" else "accepted"
        else:
            live = True
            detail = "published"
        if self.status != "draft" and not url:
            raise PublishError("Ghost accepted the post but returned no url or slug")
        LOG.info("Sent %s to ghost | %s", item.id, url or detail)
        return PublishOutcome(
            platform=self.platform,
            ok=True,
            mode=self.mode,
            live=live,
            url=url,
            external_id=str(created.get("id") or ""),
            detail=detail,
            words=int(payload.get("words") or 0),
        )

    def check(self) -> Dict[str, Any]:
        described = super().check()
        described["admin_url"] = self.admin_url
        described["status"] = self.status
        described["site_url"] = self.site_url
        return described


def build(config, *, dry_run: bool = False) -> GhostPublisher:
    """Entry point used by the publisher factory."""
    return GhostPublisher(config, dry_run=dry_run)
