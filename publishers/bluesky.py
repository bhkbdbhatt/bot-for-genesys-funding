"""Bluesky publisher - AT Protocol post with an external link card.

Bluesky has no long-form article API; a post is at most 300 graphemes. So this
publisher does the honest thing: it reduces the article to a title line, a
one-line summary and the canonical link, and attaches an
``app.bsky.embed.external`` card so the link renders as a proper preview.

Flow (two XRPC calls, no SDK dependency):

    POST /xrpc/com.atproto.server.createSession   {identifier, password}
    POST /xrpc/com.atproto.repo.createRecord      {repo, collection, record}

Credentials: ``BLUESKY_HANDLE`` (or ``platforms.bluesky.handle``) and an app
password in ``BLUESKY_APP_PASSWORD`` - never the account's main password.
The session is created per publish (sessions are cheap and this keeps the
publisher stateless), and the returned ``at://`` URI is rewritten to the human
``https://bsky.app/profile/<did>/post/<rkey>`` link.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict

from logging_setup import get_logger
from models import ContentItem
from publishers.base import BasePublisher, PublishError, PublishOutcome

LOG = get_logger("publisher-bluesky")

DEFAULT_SERVICE = "https://bsky.social"
POST_COLLECTION = "app.bsky.feed.post"
#: Bluesky's limit is 300 graphemes; characters are a close, conservative
#: proxy for the ASCII-heavy articles this tool produces.
TEXT_LIMIT = 300


def plain_text(markdown: str, limit: int = TEXT_LIMIT) -> str:
    """Collapse markdown to a single plain-text line, clipped to ``limit``."""
    lines: list[str] = []
    for raw in markdown.splitlines():
        line = raw.strip()
        if not line or line.startswith("```") or line.startswith("|"):
            continue
        line = re.sub(r"^#{1,6}\s+", "", line)  # keep the heading's words
        line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"[*_`>]+", "", line)
        line = re.sub(r"^[-*]\s+", "", line)
        lines.append(line)
    text = " ".join(lines)
    text = re.sub(r"https?://\S+", "", text)  # the link lives in the embed card
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


class BlueskyPublisher(BasePublisher):
    platform = "bluesky"

    def __init__(self, config, *, dry_run: bool = False) -> None:
        super().__init__(config, dry_run=dry_run, platform_id=self.platform)
        self.service = self.opt_str("service", DEFAULT_SERVICE).rstrip("/")

    # -- configuration ----------------------------------------------------- #

    @property
    def handle(self) -> str:
        from config_loader import resolve_secret

        return self.opt_str("handle") or (resolve_secret("BLUESKY_HANDLE") or "")

    def check_ready(self) -> tuple[bool, str]:
        if not self.handle:
            return False, "platforms.bluesky.handle is empty and BLUESKY_HANDLE is not set"
        if not self.token:
            return False, f"environment variable {self.token_env} is not set"
        if not self.service.startswith("https://"):
            return False, f"platforms.bluesky.service must be https (got {self.service[:40]!r})"
        return True, "ok"

    # -- session ----------------------------------------------------------- #

    def create_session(self) -> Dict[str, Any]:
        response = self._request(
            "POST",
            f"{self.service}/xrpc/com.atproto.server.createSession",
            json_body={"identifier": self.handle, "password": self.token},
            headers={"Content-Type": "application/json"},
            label=f"bluesky createSession {self.handle}",
        )
        body = self._json(response)
        if not body.get("accessJwt") or not body.get("did"):
            raise PublishError("bluesky createSession returned no accessJwt/did")
        return body

    # -- payload ----------------------------------------------------------- #

    def build_record(self, item: ContentItem, payload: Dict[str, Any], *, did: str) -> Dict[str, Any]:
        title = str(payload.get("title") or item.title).strip()
        summary = str(payload.get("summary") or item.summary or "").strip()
        url = str(payload.get("canonical_url") or item.canonical_url or "").strip()

        text = plain_text(title)
        if summary:
            clipped = plain_text(summary, limit=max(TEXT_LIMIT - len(text) - 2, 40))
            text = f"{text}\n\n{clipped}" if clipped else text
        if url:
            room = TEXT_LIMIT - len(text) - 1
            if room > 0:
                text = f"{text}\n{url[:room]}"

        record: Dict[str, Any] = {
            "$type": POST_COLLECTION,
            "text": text[:TEXT_LIMIT],
            "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }
        if url:
            record["embed"] = {
                "$type": "app.bsky.embed.external",
                "external": {
                    "uri": url,
                    "title": title[:300],
                    "description": summary[:300],
                },
            }
        return record

    # -- delivery ---------------------------------------------------------- #

    def _deliver(self, item: ContentItem, payload: Dict[str, Any]) -> PublishOutcome:
        session = self.create_session()
        record = self.build_record(item, payload, did=str(session["did"]))
        response = self._request(
            "POST",
            f"{self.service}/xrpc/com.atproto.repo.createRecord",
            json_body={
                "repo": str(session["did"]),
                "collection": POST_COLLECTION,
                "record": record,
                "validate": True,
            },
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {session['accessJwt']}",
            },
            label=f"bluesky createRecord {item.id}",
        )
        body = self._json(response)
        uri = str(body.get("uri") or "")
        url = _at_uri_to_web(uri, default_handle=str(session.get("handle") or self.handle))
        if not url:
            raise PublishError("bluesky accepted the post but returned no at:// uri")
        LOG.info("Posted %s to bluesky | %s", item.id, url)
        return PublishOutcome(
            platform=self.platform,
            ok=True,
            mode=self.mode,
            live=True,
            url=url,
            external_id=uri,
            detail="posted",
            words=int(payload.get("words") or 0),
        )

    def check(self) -> Dict[str, Any]:
        described = super().check()
        described["handle"] = self.handle
        described["service"] = self.service
        described["text_limit"] = TEXT_LIMIT
        return described


def _at_uri_to_web(uri: str, *, default_handle: str = "") -> str:
    """``at://did/coll/rkey`` -> ``https://bsky.app/profile/<did_or_handle>/post/<rkey>``."""
    match = re.match(r"^at://([^/]+)/([^/]+)/(.+)$", uri or "")
    if not match:
        return ""
    repo, collection, rkey = match.groups()
    if collection != POST_COLLECTION:
        return ""
    who = repo if repo.startswith("did:") else (default_handle or repo)
    return f"https://bsky.app/profile/{who}/post/{rkey}"


def build(config, *, dry_run: bool = False) -> BlueskyPublisher:
    """Entry point used by the publisher factory."""
    return BlueskyPublisher(config, dry_run=dry_run)
