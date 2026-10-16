"""Source document (README) reading: parsing, merging, fail-soft contract."""

from __future__ import annotations

import sources
from content_engine import compose, render_prompt

README = """\
# Widget Forge

A short summary of what Widget Forge actually does, in one sentence.

## Features

- Parses arbitrary widget definitions
- Emits typed artefacts for six languages
- Diffs two widget sets without false positives

## Install

```
pip install widget-forge
```

See https://github.com/acme/widget-forge and https://docs.example.com/widget-forge.
"""


def _write_readme(tmp_path, text=README):
    path = tmp_path / "README.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_source_document_extracts_facts(tmp_path):
    document = sources.load_source_document(_write_readme(tmp_path))
    assert document.title == "Widget Forge"
    assert document.summary.startswith("A short summary")
    assert "Parses arbitrary widget definitions" in document.features
    assert "https://github.com/acme/widget-forge" in document.links
    assert document.path == (tmp_path / "README.md").resolve()


def test_merge_fills_missing_plugin_facts(sample_content):
    sample_content["plugin"]["features"] = ["existing feature"]
    document = sources.SourceDocument(
        path="README.md", text=README, title="Widget Forge",
        summary="One sentence summary.", features=["parses widgets"], links=[],
    )
    merged = sources.merge_source_into_content(document, sample_content)

    assert merged["plugin"]["features"][0] == "existing feature"
    assert "parses widgets" in merged["plugin"]["features"]


def test_merge_only_guesses_repo_url_when_absent(sample_content):
    document = sources.SourceDocument(
        path="README.md", text=README, title="", summary="", features=[],
        links=["https://github.com/acme/widget-forge"],
    )
    merged = sources.merge_source_into_content(document, sample_content)
    assert merged["plugin"]["repo_url"] == sample_content["plugin"]["repo_url"]

    sample_content["plugin"]["repo_url"] = ""
    merged = sources.merge_source_into_content(document, sample_content)
    assert merged["plugin"]["repo_url"] == "https://github.com/acme/widget-forge"


def test_read_for_content_is_noop_without_a_source(config_factory, sample_content):
    config = config_factory(
        "no-source",
        content_cfg=dict(sample_content, source_file=""),
    )
    merged, text = sources.read_for_content(config, sample_content)
    assert text == ""
    assert merged["source_text"] == ""
    assert merged["plugin"] == sample_content["plugin"]


def test_read_for_content_uses_configured_path(config_factory, sample_content, tmp_path):
    _write_readme(tmp_path)
    config = config_factory(
        "configured-source",
        content_cfg=dict(sample_content, source_file="README.md"),
    )
    merged, text = sources.read_for_content(config, sample_content)
    assert "Widget Forge" in text
    assert merged["source_text"] == text
    assert "Parses arbitrary widget definitions" in merged["plugin"]["features"]


def test_read_for_content_prefers_the_explicit_path(config_factory, sample_content, tmp_path):
    _write_readme(tmp_path)
    other = tmp_path / "OTHER.md"
    other.write_text("# Other\n\nFrom the other file.\n", encoding="utf-8")
    config = config_factory(
        "explicit-source",
        content_cfg=dict(sample_content, source_file="README.md"),
    )
    merged, text = sources.read_for_content(config, sample_content, str(other))
    assert "From the other file." in text
    assert merged["source_text"] == text


def test_read_for_content_never_raises_on_a_missing_file(config_factory, sample_content):
    config = config_factory(
        "missing-source",
        content_cfg=dict(sample_content, source_file="nope/does-not-exist.md"),
    )
    merged, text = sources.read_for_content(config, sample_content)
    assert text == ""
    assert merged["plugin"] == sample_content["plugin"]


def test_source_text_flows_into_the_prompt(sample_content, tmp_path):
    document = sources.load_source_document(_write_readme(tmp_path))
    merged = sources.merge_source_into_content(document, sample_content)
    prompt = render_prompt(merged, platform="devto")

    assert "SOURCE DOCUMENT (readme/brief material):" in prompt
    assert "# Widget Forge" in prompt
    assert "{{" not in prompt, "unresolved template tokens in the prompt"


def test_composer_picks_up_readme_features(sample_content, tmp_path):
    document = sources.load_source_document(_write_readme(tmp_path))
    merged = sources.merge_source_into_content(document, sample_content)
    draft = compose(merged, title="Widget Forge in practice", platform="devto")
    assert "Parses arbitrary widget definitions" in draft["body"]


def test_service_draft_reads_the_configured_source(config_factory, sample_content, tmp_path):
    _write_readme(tmp_path)
    config = config_factory(
        "service-source",
        content_cfg=dict(sample_content, source_file="README.md"),
        platforms_cfg={"devdojo": {"enabled": True}},
    )
    from webapp.service import draft_article

    result = draft_article(config, title="Widget Forge in practice", platforms=["devdojo"])

    assert result["renderer"] == "deterministic"
    assert "Parses arbitrary widget definitions" in result["item"]["body_markdown"]
    assert all(not row.get("problems") for row in result["gate"])
