"""Content store ledger: coverage ported from _selftest.py."""

from __future__ import annotations

import pytest

from content_store import GLOBAL_KEY, ContentStore, ContentStoreError
from models import ContentItem

LIMITS = {"devto": 1, "hashnode": 1, "coderlegion": 5, GLOBAL_KEY: 2}


def _item(item_id: str, platforms=None, body: str = "# t") -> ContentItem:
    item = ContentItem(
        id=item_id,
        title="t",
        body_markdown=body,
        platforms=platforms if platforms is not None else ["devto", "coderlegion"],
    )
    item.fingerprint = item.compute_fingerprint()
    return item


@pytest.fixture()
def store(tmp_path):
    return ContentStore(tmp_path / "content.json", rate_limits=dict(LIMITS))


def test_created_draft_and_roundtrip(store, tmp_path):
    stored, created = store.add(_item("post-one"))
    assert created and stored.id == "post-one"
    assert stored.status == "draft"
    store.save()

    reopened = ContentStore(tmp_path / "content.json", rate_limits=dict(LIMITS))
    assert reopened.get("post-one") is not None
    assert isinstance(reopened.get("post-one").publications, dict)


def test_not_publishable_before_approval(store):
    store.add(_item("post-one"))
    assert store.next_targets(5) == []


def test_full_fanout_confirm_unconfirm_cycle(store):
    store.add(_item("post-one"))
    store.approve("post-one")
    targets = store.next_targets(5)
    assert len(targets) == 2
    assert [t.platform for t in targets] == ["devto", "coderlegion"]

    store.record_publish(
        targets[0], ok=True, mode="api", live=True, url="https://dev.to/x/one", external_id="9"
    )
    store.save()
    item = store.require("post-one")
    assert item.publication("devto").is_live
    assert item.status == "approved", item.status
    assert store.used_today("devto") == 1

    remaining = store.next_targets(5)
    assert [t.platform for t in remaining] == ["coderlegion"]

    store.record_publish(remaining[0], ok=True, mode="manual", detail="written to outbox")
    store.save()
    item = store.require("post-one")
    assert item.status == "queued", item.status
    assert item.publication("coderlegion").url == ""
    assert bool(item.publication("coderlegion").queued_at)

    store.confirm("post-one", "coderlegion", url="https://coderlegion.com/1/one")
    item = store.require("post-one")
    assert item.status == "published", item.status
    assert len(item.live_urls()) == 2
    assert store.next_targets(5) == []

    with pytest.raises(ContentStoreError):
        store.confirm("post-one", "coderlegion", url="not-a-url")

    store.unconfirm("post-one", "coderlegion")
    item = store.require("post-one")
    assert item.status == "approved", item.status
    assert item.publication("coderlegion").url == ""
    assert len(store.next_targets(5)) == 1


def test_global_cap_includes_failures(store):
    store.add(_item("post-one"))
    store.approve("post-one")
    store.record_publish(store.next_targets(1)[0], ok=True, mode="api", live=True, url="https://dev.to/x")
    store.record_publish(store.next_targets(1)[0], ok=True, mode="manual")
    store.unconfirm("post-one", "coderlegion")
    store.record_publish(store.next_targets(1)[0], ok=False, mode="api", detail="boom")
    capped = store.next_targets(1)
    assert len(capped) == 1
    allowed, reason = store.can_publish(capped[0])
    assert not allowed and "global" in reason, reason
    assert store.used_today(GLOBAL_KEY) == 3, str(store.used_today(GLOBAL_KEY))


def test_missing_limit_is_refused_not_unlimited(tmp_path):
    unconfigured = ContentStore(tmp_path / "c5.json", rate_limits={})
    unconfigured.add(_item("p11", platforms=["devdojo"]))
    unconfigured.approve("p11")
    allowed, why = unconfigured.can_publish(unconfigured.next_targets(1)[0])
    assert not allowed and "no daily publish limit" in why, why


def test_manual_cooldown_and_unconfirm_override(tmp_path):
    store2 = ContentStore(tmp_path / "c2.json", rate_limits={"coderlegion": 5, GLOBAL_KEY: 50})
    store2.add(_item("p2", platforms=["coderlegion"], body="# t\n\nbody"))
    store2.approve("p2")
    store2.record_publish(store2.next_targets(1)[0], ok=True, mode="manual")
    retry = store2.next_targets(1)
    assert len(retry) == 1
    ok2, reason2 = store2.can_publish(retry[0], min_hours_between=24)
    assert not ok2 and "cooling" in reason2, reason2
    ok3, reason3 = store2.can_publish(retry[0])
    assert ok3, reason3
    store2.unconfirm("p2", "coderlegion")
    ok5, reason5 = store2.can_publish(store2.next_targets(1)[0], min_hours_between=24)
    assert ok5, reason5


def test_failed_attempt_cools_down(tmp_path):
    store2b = ContentStore(tmp_path / "c2b.json", rate_limits={"devto": 5, GLOBAL_KEY: 50})
    store2b.add(_item("p2b", platforms=["devto"]))
    store2b.approve("p2b")
    store2b.record_publish(store2b.next_targets(1)[0], ok=False, mode="api", detail="HTTP 401")
    allowed, reason = store2b.can_publish(store2b.next_targets(1)[0], min_hours_between=24)
    assert not allowed and "cooling" in reason, reason


def test_corrupt_file_quarantined_and_self_heals(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    store3 = ContentStore(bad, rate_limits={"devto": 1})
    assert (tmp_path / "bad.json.corrupt").is_file()
    assert store3.path.is_file() and store3.all() == []


def test_refresh_keeps_publication_history(tmp_path):
    store4 = ContentStore(tmp_path / "c4.json", rate_limits={"devto": 5, GLOBAL_KEY: 50})
    store4.add(_item("p4", platforms=["devto"]))
    store4.approve("p4")
    store4.record_publish(
        store4.next_targets(1)[0], ok=True, mode="api", live=True, url="https://dev.to/y"
    )
    refreshed = ContentItem(id="p4", title="t", body_markdown="# t v2", platforms=["devto", "hashnode"])
    merged, was_new = store4.add(refreshed)
    assert was_new is False
    assert merged.publication("devto").url == "https://dev.to/y"
    assert merged.body_markdown == "# t v2"
    assert merged.platforms == ["devto", "hashnode"]


def test_add_rejects_bad_input(store):
    from models import PLATFORM_IDS

    with pytest.raises(ContentStoreError):
        store.add(ContentItem(id="p9", title="t", body_markdown="# t", platforms=["nope"]))
    with pytest.raises(ContentStoreError):
        store.add(ContentItem(id="p10", title="t", body_markdown="# t", platforms=[]))
    with pytest.raises(ContentStoreError):
        store.add(ContentItem(id="p11", title="t", body_markdown="   ", platforms=[PLATFORM_IDS[0]]))
