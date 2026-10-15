"""Markdown -> HTML renderer: coverage ported from _selftest.py."""

from __future__ import annotations

from publishers.markdown_html import markdown_to_html, render_inline, strip_front_matter

SAMPLE = "\n".join(
    [
        "# Title here",
        "",
        "Intro with **bold**, *italic*, `code` and a [link](https://example.com/a).",
        "",
        "## Section one",
        "",
        "- first item",
        "- second item",
        "",
        "1. one",
        "2. two",
        "",
        "> quoted line",
        "> second quote line",
        "",
        "```python",
        "def f(x):",
        "    return x < 1 and x > 0",
        "```",
        "",
        "| A | B |",
        "|---|---|",
        "| 1 | 2 |",
        "",
        "---",
        "",
        "Tail with https://bare.example.com/p autolinked.",
    ]
)


def test_block_structure():
    html = markdown_to_html(SAMPLE)
    assert "<h1>Title here</h1>" in html
    assert "<h2>Section one</h2>" in html
    assert "<strong>bold</strong>" in html
    assert "<em>italic</em>" in html
    assert "<code>code</code>" in html
    assert '<a href="https://example.com/a">link</a>' in html
    assert "<ul><li>first item</li><li>second item</li></ul>" in html
    assert "<ol><li>one</li><li>two</li></ol>" in html
    assert html.count("<blockquote>") == 1
    assert "return x &lt; 1" in html
    assert 'class="language-python"' in html
    assert "<table><thead><tr><th>A</th><th>B</th></tr></thead>" in html
    assert "<hr>" in html
    assert '<a href="https://bare.example.com/p">' in html


def test_xss_is_neutralised():
    danger = markdown_to_html("<script>alert(1)</script> [x](javascript:alert(1)) <b>y</b>")
    assert "&lt;script&gt;" in danger and "<script>" not in danger
    assert 'href="javascript' not in danger.lower()
    assert "<a " not in danger
    assert "&lt;b&gt;y&lt;/b&gt;" in danger


def test_data_uri_filtered():
    data_uri = markdown_to_html("[x](data:text/html;base64,PHNjcmlwdD4=)")
    assert 'href="data:' not in data_uri.lower()
    assert "<a " not in data_uri
    img = markdown_to_html("![a](data:image/svg+xml;base64,PHN2Zz4=)")
    assert 'src="data:' not in img.lower()


def test_front_matter():
    front, body = strip_front_matter("---\ntitle: x\n---\n# H\n\ntext")
    assert front.strip() == "title: x"
    assert body.startswith("# H")
    assert strip_front_matter("# H") == ("", "# H")


def test_inline_empty_is_safe():
    assert render_inline("") == ""
