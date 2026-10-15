"""Shared fixtures. The project is a flat layout, so the repo root goes on sys.path."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture()
def clean_env(monkeypatch):
    """Strip every credential-like env var publishers/senders read."""
    import platforms

    for spec in platforms.PLATFORMS.values():
        if spec.token_env:
            monkeypatch.delenv(spec.token_env, raising=False)
    for extra in ("GITLAB_TOKEN", "SMTP_PASSWORD", "FORUM_API_KEY", "WEBHOOK_SECRET"):
        monkeypatch.delenv(extra, raising=False)


@pytest.fixture()
def sample_content():
    """A content config that composes a gate-passing article."""
    import content_engine as ce

    return {
        "audience": "Genesys Cloud developers",
        "angle": "Stop rebuilding the same integration in every project.",
        "disclosure": "I maintain this project.",
        "disclosure_required": True,
        "tags": ["Genesys", "genesys-cloud", "Open Source", "genesys"],
        "sections": list(ce.DEFAULT_SECTIONS),
        "min_words": 350,
        "max_words": 1800,
        "summary_max_chars": 300,
        "max_title_words": 12,
        "forbid_words": ["kubernetes"],
        "plugin": {
            "name": "Genesys Cloud Community Plugin",
            "description": "Open-source plugin.",
            "value_prop": "Stop rebuilding the same Genesys Cloud integration in every project.",
            "features": ["Drop-in API wrapper", "Event-driven architecture"],
            "repo_url": "https://github.com/purecloudlabs/community-plugin-example",
            "appfoundry_url": "https://appfoundry.genesys.cloud/",
            "demo_url": "",
            "maintainer_name": "Maintainer",
            "maintainer_email": "you@example.com",
            "cta": "Would a 15-minute walkthrough be useful?",
            "ask": "We are looking for sponsors.",
            "license": "MIT",
        },
    }


@pytest.fixture()
def composed_draft(sample_content):
    import content_engine as ce

    return ce.compose(sample_content, title="A tested integration pattern", topic="integration")


@pytest.fixture()
def config_factory(tmp_path, repo_root):
    """Build an isolated Config from config.example.yaml with overrides applied."""
    import yaml

    from config_loader import Config

    base_doc = yaml.safe_load((repo_root / "config.example.yaml").read_text(encoding="utf-8"))

    def factory(name: str, mutate=None, content_cfg=None, platforms_cfg=None, publishing=None):
        import copy

        document = copy.deepcopy(base_doc)
        document.setdefault("paths", {})
        document["paths"]["content_file"] = f"{name}-content.json"
        document["paths"]["tracker_file"] = f"{name}-sponsors.json"
        if content_cfg is not None:
            document["content"] = dict(content_cfg, enabled=True)
        if platforms_cfg is not None:
            document["platforms"] = platforms_cfg
        if publishing is not None:
            document["publishing"] = dict(document.get("publishing") or {}, **publishing)
        document.setdefault("rate_limits", {})
        document["rate_limits"].update(
            {
                "max_publishes_per_day": 100,
                "min_hours_between_platform_posts": 0,
            }
        )
        if mutate:
            mutate(document)
        target = tmp_path / f"config-{name}.yaml"
        target.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return Config.load(target, create_from_example=False)

    return factory
