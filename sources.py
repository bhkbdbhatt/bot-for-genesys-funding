"""Source document helpers for content generation.

This module provides lightweight utilities to read README/source files and
extract structured facts that can be fed into the content engine. Extraction is
deliberately deterministic and uses only the standard library.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from logging_setup import get_logger

LOG = get_logger("sources")


@dataclass
class SourceDocument:
    """Structured representation of a source document (e.g. README)."""

    path: Path
    text: str
    title: str = ""
    summary: str = ""
    features: List[str] = field(default_factory=list)
    links: List[str] = field(default_factory=list)


def _extract_title(text: str) -> str:
    """Extract title from H1 or first non-empty line."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def _extract_summary(text: str) -> str:
    """Extract a short summary: the first prose line after the title."""
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("#"):
        lines = lines[1:]
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            break
        if stripped.startswith("![") or stripped.startswith("<img"):
            continue
        if re.match(r"^([-*]\s|\d+\.\s)", stripped):
            break
        return stripped
    return ""


def _extract_features(text: str) -> List[str]:
    features: List[str] = []
    in_list = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            in_list = False
            continue
        if stripped.startswith("- ") or stripped.startswith("* ") or re.match(r"^\d+\.\s", stripped):
            in_list = True
            item = re.sub(r"^[-*]\s+|\d+\.\s+", "", stripped).strip()
            if item and len(item) > 3:
                features.append(item)
            continue
        if in_list and stripped.startswith("#"):
            break
    return features[:10]


def _extract_links(text: str) -> List[str]:
    links: List[str] = []
    for match in re.finditer(r"https?://[^\s<>()\[\]]+", text):
        url = match.group(0).rstrip(".,;:")
        links.append(url)
    seen: List[str] = []
    for url in links:
        if url not in seen:
            seen.append(url)
            if len(seen) >= 20:
                break
    return seen


def load_source_document(path: Path) -> SourceDocument:
    """Read and parse a source document."""
    try:
        text = path.expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        LOG.error("Failed to read source document %s: %s", path, exc)
        raise

    return SourceDocument(
        path=path.expanduser().resolve(),
        text=text,
        title=_extract_title(text),
        summary=_extract_summary(text),
        features=_extract_features(text),
        links=_extract_links(text),
    )


def merge_source_into_content(source: SourceDocument, content: dict) -> dict:
    """Merge extracted source facts into content data (non-destructive)."""
    merged = dict(content) if content else {}
    plugin = dict(merged.get("plugin") or {})
    if not plugin.get("name") and source.title:
        plugin["name"] = source.title
    if not plugin.get("description") and source.summary:
        plugin["description"] = source.summary
    if not plugin.get("value_prop") and source.summary:
        plugin["value_prop"] = source.summary

    existing_features = list(plugin.get("features") or [])
    for feature in source.features:
        if feature not in existing_features:
            existing_features.append(feature)
    plugin["features"] = existing_features[:12]

    for url in source.links:
        if not plugin.get("repo_url") and ("/github.com/" in url or url.endswith(".git")):
            plugin["repo_url"] = url
        elif not plugin.get("demo_url") and ("demo" in url or "example" in url or "app" in url):
            plugin["demo_url"] = url

    merged["plugin"] = plugin
    merged["source_text"] = source.text
    return merged


def resolve_source_path(config, explicit: str = "") -> Optional[Path]:
    """Resolve the source document path from a CLI/UI value or the config.

    An explicit value is cwd-relative first (that is where a shell command
    runs from), while a path that comes from ``content.source_file`` is
    resolved against the profile's config directory first, because a profile
    lives in its own directory. Returns a path even when it does not exist, so
    the caller can report it.
    """
    raw = str(explicit or "").strip()
    from_config = not raw
    if from_config and config is not None and hasattr(config, "str"):
        raw = config.str("content.source_file") or config.str("content.readme")
    if not raw:
        return None

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute() and config is not None and hasattr(config, "base_dir"):
        base_relative = Path(config.base_dir) / candidate
        if from_config:
            # A configured path belongs to the profile directory, not the
            # caller's working directory.
            return base_relative if base_relative.is_file() else candidate
        if candidate.is_file():
            return candidate
        if base_relative.is_file():
            return base_relative
    return candidate


def read_for_content(config, content, explicit: str = ""):
    """Merge the configured source document into ``content``.

    Returns ``(merged_content, source_text)``. Failures are never fatal: a
    missing or unreadable source logs a warning and the brief alone is used,
    which keeps the deterministic composer's fail-soft contract.
    """
    path = resolve_source_path(config, explicit)
    if path is None:
        merged = dict(content or {})
        merged["source_text"] = ""
        return merged, ""
    try:
        document = load_source_document(path)
    except OSError as exc:
        LOG.warning("Could not read source %s: %s", path, exc)
        merged = dict(content or {})
        merged["source_text"] = ""
        return merged, ""
    merged = merge_source_into_content(document, content or {})
    return merged, document.text


__all__ = [
    "SourceDocument",
    "load_source_document",
    "merge_source_into_content",
    "read_for_content",
    "resolve_source_path",
]
