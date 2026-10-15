"""Content composer + quality gate: coverage ported from _selftest.py."""

from __future__ import annotations

import content_engine as ce
from platforms import PLATFORMS


def test_compose_produces_gate_passing_article(sample_content):
    draft = ce.compose(sample_content, title="A tested integration pattern", topic="integration")
    words = len(draft["body"].split())
    assert draft["body"].startswith("# ")
    assert "I maintain this project" in draft["body"]
    assert "github.com/purecloudlabs" in draft["body"]
    assert draft["body"].count("\n## ") >= 2
    assert words >= 350, f"composed {words} words"
    problems = ce.validate(
        sample_content, title=draft["title"], body=draft["body"], platform="devto"
    )
    assert problems == [], problems


def test_gate_flags_thin_body(sample_content):
    thin = ce.validate(
        sample_content,
        title="t",
        body="# T\n\n## a\n\n## b\n\nhttps://github.com/purecloudlabs/community-plugin-example",
        platform="devto",
    )
    assert any("word minimum" in p for p in thin), thin


def test_gate_flags_hype_and_forbid_words(sample_content):
    hype_body = (
        "# T\n\n## a\n\n## b\n\n"
        + ("word " * 400)
        + "\n\nThis is a seamless integration.\n\n"
        + "https://github.com/purecloudlabs/community-plugin-example\n\n"
        + "I maintain this project."
    )
    hype = ce.validate(sample_content, title="t", body=hype_body, platform="devto")
    assert any("banned wording" in p for p in hype), hype

    forbidden = ce.validate(
        sample_content,
        title="t",
        body=hype_body.replace("seamless", "runs on kubernetes"),
        platform="devto",
    )
    assert any("kubernetes" in p for p in forbidden), forbidden


def test_gate_flags_missing_disclosure_and_repo_link(sample_content):
    pad = "word " * 400
    nodisc = ce.validate(
        sample_content,
        title="t",
        body="# T\n\n## a\n\n## b\n\n" + pad + "\n\nhttps://github.com/purecloudlabs/community-plugin-example",
        platform="devto",
    )
    assert any("disclosure" in p for p in nodisc), nodisc

    norepo = ce.validate(
        sample_content,
        title="t",
        body="# T\n\n## a\n\n## b\n\n" + pad + "\n\nI maintain this project.",
        platform="devto",
    )
    assert any("link to the project" in p for p in norepo), norepo


def test_tags_and_title_clamped():
    assert ce.normalise_tags(["Genesys Cloud", "#DevTo", "genesys"], "devto") == [
        "genesyscloud",
        "devto",
        "genesys",
    ]
    tag_limit_devto = PLATFORMS["devto"].tag_limit
    tag_limit_hashnode = PLATFORMS["hashnode"].tag_limit
    assert len(ce.normalise_tags(["a", "b", "c", "d", "e", "f"], "devto")) == tag_limit_devto
    assert len(ce.normalise_tags(["a", "b", "c", "d", "e", "f"], "hashnode")) == tag_limit_hashnode
    assert ce.clamp_title("one two three four five", 3) == "one two three"
