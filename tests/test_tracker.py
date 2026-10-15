"""Sponsor tracker regressions: coverage ported from _selftest.py."""

from __future__ import annotations

from tracker import SponsorTracker


def test_seeding_send_counters_cooldown_and_cap(tmp_path):
    tpath = tmp_path / "sponsors.json"
    tracker = SponsorTracker(
        tpath,
        seed_sponsors=[{"name": "Acme", "email": "a@b.com"}],
        rate_limits={"email": 2, "forum": 1},
    )
    assert tracker.get("Acme") is not None
    ok, _ = tracker.can_send(tracker.require("Acme"))
    assert ok

    tracker.record_send("Acme", "email", ok=True)
    tracker.save()
    assert tracker.used_today("email") == 1

    ok2, reason = tracker.can_send(tracker.require("Acme"), min_hours_between_attempts=72)
    assert not ok2 and "cooling" in reason, reason

    tracker.record_send("Acme", "email", ok=True)
    tracker.save()
    ok3, reason3 = tracker.can_send(tracker.require("Acme"))
    assert not ok3 and "limit reached" in reason3, reason3
    assert tracker.require("Acme").status == "contacted"

    summary = tracker.summary()
    assert set(summary) >= {
        "tracker_file",
        "total",
        "by_status",
        "by_channel",
        "daily_usage",
        "recent_history",
    }
    assert set(summary["daily_usage"]) == {"email", "forum"}


def test_counters_and_history_survive_reload(tmp_path):
    tpath = tmp_path / "sponsors.json"
    tracker = SponsorTracker(
        tpath, seed_sponsors=[{"name": "Acme", "email": "a@b.com"}], rate_limits={"email": 2, "forum": 1}
    )
    tracker.record_send("Acme", "email", ok=True)
    tracker.record_send("Acme", "email", ok=True)
    tracker.save()

    reloaded = SponsorTracker(tpath, rate_limits={"email": 2, "forum": 1})
    assert reloaded.used_today("email") == 2
    assert len(reloaded.history()) >= 2


def test_quarantines_corrupt_file(tmp_path):
    tbad = tmp_path / "bad.json"
    tbad.write_text("}}}", encoding="utf-8")
    SponsorTracker(tbad, rate_limits={"email": 1, "forum": 1})
    assert (tmp_path / "bad.json.corrupt").is_file()
