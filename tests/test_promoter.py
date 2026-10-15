"""Promoter tick: dry-run purity, manual hand-off, gate, platform report.

Ported from _selftest.py; isolated configs via the config_factory fixture.
"""

from __future__ import annotations

import contextlib
import io
import json

import pytest

from models import ContentItem
from promoter import Promoter, build_store

CANONICAL_PLATFORMS = {"devto", "coderlegion", "devdojo"}


@pytest.fixture()
def dry_config(config_factory, sample_content):
    return config_factory(
        "dry",
        content_cfg=sample_content,
        platforms_cfg={pid: {"enabled": True} for pid in CANONICAL_PLATFORMS},
        publishing={"dry_run": True, "batch_size": 5, "require_approval": True},
    )


def _draft_item(item_id: str, platforms, composed):
    item = ContentItem(
        id=item_id,
        title=composed["title"],
        body_markdown=composed["body"],
        summary=composed["summary"],
        platforms=platforms,
        tags=["genesys"],
    )
    item.fingerprint = item.compute_fingerprint()
    return item


def test_config_and_store_resolution(dry_config, tmp_path):
    assert dry_config.publishing_enabled is True
    assert set(dry_config.platforms_enabled()) == CANONICAL_PLATFORMS
    store = build_store(dry_config)
    assert store.path.parent == tmp_path.resolve(), str(store.path)


def test_dry_run_purity(dry_config, composed_draft):
    store = build_store(dry_config)
    promoter = Promoter(dry_config, store, dry_run=True, batch_size=5)
    store.add(_draft_item("dry-one", ["devto", "coderlegion"], composed_draft))
    store.approve("dry-one")

    result = promoter.tick()
    assert result.attempted == 2, json.dumps(result.to_dict())
    assert result.published == 0
    assert store.used_today("devto") == 0
    assert len([n for n in result.notes if "dry run" in n]) == 2, str(result.notes)
    assert not store.require("dry-one").publication("devto").is_live
    assert store.require("dry-one").publication("devto").attempts == 0
    assert promoter.tick().attempted == 2
    outbox = store.path.parent / "content_outbox" / "coderlegion" / "dry-one.md"
    assert not outbox.exists()


def test_unapproved_draft_not_attempted(dry_config, composed_draft):
    store = build_store(dry_config)
    promoter = Promoter(dry_config, store, dry_run=True, batch_size=5)
    store.add(ContentItem(id="draft-one", title="x", body_markdown="# x", platforms=["devto"]))
    result = promoter.tick()
    assert all(r.item_id != "draft-one" for r in result.records)


def test_manual_live_run(config_factory, sample_content, composed_draft):
    cfg_live = config_factory(
        "live",
        content_cfg=sample_content,
        platforms_cfg={pid: {"enabled": True} for pid in CANONICAL_PLATFORMS},
        publishing={"dry_run": False, "batch_size": 5, "require_approval": True},
    )
    store2 = build_store(cfg_live)
    store2.add(
        _draft_item("manual-one", ["devdojo", "coderlegion"], composed_draft)
    )
    store2.approve("manual-one")
    live = Promoter(cfg_live, store2, dry_run=False, batch_size=5)
    live.request_delay = 0.0

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        res = live.tick(platforms=["devdojo", "coderlegion"])
    printed = captured.getvalue()

    assert res.queued == 2, json.dumps(res.to_dict())
    assert res.published == 0
    assert all(not r.live for r in res.records)
    stored = store2.require("manual-one")
    assert stored.status == "queued", stored.status

    outbox = store2.path.parent / "content_outbox" / "devdojo" / "manual-one.md"
    assert outbox.is_file()
    text = outbox.read_text(encoding="utf-8")
    assert "submit at:" in text
    assert "# " + composed_draft["title"] in text
    assert store2.used_today("devdojo") == 1
    assert "MANUAL POST" in printed and "content confirm" in printed
    assert "DevDojo" in printed and "CoderLegion" in printed


def test_missing_credential_is_skip_not_crash(config_factory, sample_content, composed_draft):
    cfg = config_factory(
        "needs-token",
        content_cfg=sample_content,
        platforms_cfg={"devto": {"enabled": True}},
        publishing={"dry_run": False, "batch_size": 5, "require_approval": True},
    )
    store2 = build_store(cfg)
    store2.add(_draft_item("needs-token", ["devto"], composed_draft))
    store2.approve("needs-token")
    live = Promoter(cfg, store2, dry_run=False, batch_size=5)
    live.request_delay = 0.0
    with contextlib.redirect_stdout(io.StringIO()):
        res3 = live.tick(platforms=["devto"])
    assert res3.skipped == 1 and res3.failed == 0, json.dumps(res3.to_dict())
    assert "DEVTO_API_KEY" in res3.notes[0], str(res3.notes)
    assert store2.require("needs-token").publication("devto").status == "pending"
    assert store2.used_today("devto") == 0


def test_quality_gate_blocks_thin_draft(config_factory, sample_content):
    cfg_gate = config_factory(
        "gate",
        content_cfg=sample_content,
        platforms_cfg={"devdojo": {"enabled": True}},
        publishing={"dry_run": False, "batch_size": 5, "require_approval": True},
    )
    store3 = build_store(cfg_gate)
    store3.add(ContentItem(id="thin", title="Thin", body_markdown="# Thin\n\nshort", platforms=["devdojo"]))
    store3.approve("thin")
    gate = Promoter(cfg_gate, store3, dry_run=False, batch_size=5)
    gate.request_delay = 0.0
    with contextlib.redirect_stdout(io.StringIO()):
        res4 = gate.tick(platforms=["devdojo"])
    assert res4.skipped == 1 and res4.attempted == 0, json.dumps(res4.to_dict())
    assert "quality gate" in res4.notes[0]
    assert store3.used_today("devdojo") == 0

    cfg_lax = config_factory(
        "lax",
        content_cfg=sample_content,
        platforms_cfg={"devdojo": {"enabled": True}},
        publishing={"dry_run": True, "batch_size": 5, "enforce_quality_gate": False},
    )
    store4 = build_store(cfg_lax)
    store4.add(ContentItem(id="thin2", title="Thin", body_markdown="# Thin\n\nshort", platforms=["devdojo"]))
    store4.approve("thin2")
    lax = Promoter(cfg_lax, store4, dry_run=True, batch_size=5)
    res5 = lax.tick(platforms=["devdojo"])
    assert res5.attempted == 1, json.dumps(res5.to_dict())


def test_platform_report(dry_config, monkeypatch):
    from models import PLATFORM_IDS
    from publishers import check_all

    monkeypatch.delenv("HASHNODE_PAT", raising=False)
    report = check_all(dry_config)
    assert len(report) == len(PLATFORM_IDS), str(len(report))
    by_id = {entry["platform"]: entry for entry in report}
    assert by_id["devto"]["ready"] is False
    assert "DEVTO_API_KEY" in by_id["devto"]["reason"], by_id["devto"]["reason"]
    assert "HASHNODE_PAT" in by_id["hashnode"]["reason"]
    assert by_id["devdojo"]["ready"] is True
    assert by_id["medium"]["legacy"] is True
    assert all("Bearer" not in str(entry.get("reason", "")) for entry in report)

    monkeypatch.setenv("HASHNODE_PAT", "test-token-not-real")
    by_id2 = {entry["platform"]: entry for entry in check_all(dry_config)}
    assert "publication_id" in by_id2["hashnode"]["reason"], by_id2["hashnode"]["reason"]
