"""How the API serves a step's page by its extension (tech-stack decision 58): HTML as written,
Markdown rendered inert, and anything else as plain text, never a type guessed from the name."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from orchestrator.api.page_formats import (
    RENDERERS,
    NotText,
    RenderTooLarge,
    _PageMarkdown,
    render,
)
from runtime.adapters._cli.environment import PAGE_RULES

HTML = "text/html; charset=utf-8"
TEXT = "text/plain; charset=utf-8"
SVG = "data:image/svg+xml;base64,PHN2Zy8+"


def _main(markdown: str) -> str:
    """A Markdown page's rendered body, without its fixed shell."""
    page = render("summary.md", markdown.encode()).body.decode()
    return page.split("<main>", 1)[1].removesuffix("</main></body></html>")


@pytest.mark.parametrize(
    ("path", "media_type"),
    [
        ("summary.html", HTML),
        ("docs/Summary.HTML", HTML),
        ("summary.md", HTML),
        ("specs/12/SUMMARY.MD", HTML),
        ("notes.txt", TEXT),
        ("diagram.svg", TEXT),
        ("page.xhtml", TEXT),
        ("summary", TEXT),
        ("summary.{slug}", TEXT),
    ],
)
def test_the_extension_alone_picks_the_media_type(path: str, media_type: str) -> None:
    assert render(path, b"<svg onload=x>").media_type == media_type


def test_html_and_raw_text_are_served_as_their_exact_bytes() -> None:
    body = b"<h1>Plan</h1>\n<script>x</script>\n"
    assert render("summary.html", body).body == body
    assert render("notes.txt", body).body == body


def test_html_is_never_decoded() -> None:
    assert render("summary.html", b"\xff\xfe<h1>").body == b"\xff\xfe<h1>"


@pytest.mark.parametrize("path", ["notes.txt", "summary.md", "image.png"])
def test_a_text_type_refuses_bytes_that_are_not_utf8(path: str) -> None:
    with pytest.raises(NotText):
        render(path, b"\x89PNG\r\n\x1a\n\xff")


def test_a_byte_order_mark_does_not_hide_a_markdown_pages_first_heading() -> None:
    assert _main("\ufeff# Title\n") == "<h1>Title</h1>\n"
    assert render("notes.txt", "\ufeffhi".encode()).body == "\ufeffhi".encode()


def test_markdown_renders_inside_a_fixed_shell_titled_by_its_path() -> None:
    page = render('docs/<x>&".md', b"# Plan\n").body.decode()
    assert page.startswith('<!doctype html><html lang="en"><head><meta charset="utf-8">')
    assert "<title>docs/&lt;x&gt;&amp;&quot;.md</title>" in page
    assert page.endswith("<body><main><h1>Plan</h1>\n</main></body></html>")


def test_an_empty_markdown_file_is_an_empty_page() -> None:
    assert _main("") == ""


def test_markdown_renders_tables_and_strikethrough() -> None:
    body = _main("| a | b |\n|---|---|\n| 1 | ~~2~~ |\n")
    assert "<table>" in body
    assert "<td><s>2</s></td>" in body


def test_raw_html_in_markdown_is_shown_as_text() -> None:
    assert _main("<script>alert(1)</script>\n") == "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>\n"


def test_a_fenced_block_shows_its_markup_as_code() -> None:
    assert _main("```html\n<script>alert(1)</script>\n```\n") == (
        '<pre><code class="language-html">&lt;script&gt;alert(1)&lt;/script&gt;\n</code></pre>\n'
    )


@pytest.mark.parametrize("link", ["https://github.com/acme/app/pull/7", "docs/spec.md"])
def test_every_markdown_link_opens_a_new_tab(link: str) -> None:
    assert _main(f"[go]({link})\n") == (
        f'<p><a href="{link}" target="_blank" rel="noopener noreferrer">go</a></p>\n'
    )


@pytest.mark.parametrize(
    "target",
    [
        "javascript:alert(1)",
        "data:text/html;base64,PGgxPmhpPC9oMT4=",
        SVG,
        "data:image/png;base64,AA==",
    ],
)
def test_a_markdown_link_never_points_at_script_or_a_data_url(target: str) -> None:
    for markdown in (f"[go]({target})\n", f"<{target}>\n", f"[go][r]\n\n[r]: {target}\n"):
        assert "href" not in _main(markdown), markdown


@pytest.mark.parametrize("target", [SVG, "DATA:image/png;base64,AA=="])
def test_a_link_to_a_data_url_reads_as_plain_text(target: str) -> None:
    assert _main(f"[go]({target}) then [next](https://e.example)\n") == (
        '<p>go then <a href="https://e.example" target="_blank" rel="noopener noreferrer">'
        "next</a></p>\n"
    )
    for markdown in (f"<{target}>\n", f"[go][r]\n\n[r]: {target}\n"):
        assert "<a" not in _main(markdown), markdown


def test_a_markdown_image_may_be_an_svg_or_a_raster_data_url() -> None:
    body = _main(f"![flow]({SVG}) ![bar](data:image/png;base64,AA==)\n")
    assert f'<img src="{SVG}" alt="flow" />' in body
    assert '<img src="data:image/png;base64,AA==" alt="bar" />' in body


def test_a_percent_encoded_svg_data_url_is_an_image_too() -> None:
    src = "data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%3E%3C/svg%3E"
    assert f'<img src="{src}" alt="flow" />' in _main(f"![flow]({src})\n")


def test_a_markdown_image_is_never_an_html_data_url() -> None:
    assert "<img" not in _main("![x](data:text/html;base64,PGgxPmhpPC9oMT4=)\n")


def test_a_new_parser_builds_every_rule_chain_before_any_thread_can_race_to() -> None:
    """markdown-it builds a ruler's chains on first use, and a thread reading them while another
    builds them can get them half built: a link then renders without its new-tab target."""
    md = _PageMarkdown()
    rulers = (md.core.ruler, md.block.ruler, md.inline.ruler, md.inline.ruler2)
    assert all(ruler.__cache__ is not None for ruler in rulers)


def test_markdown_renders_the_same_from_many_threads_at_once() -> None:
    """The API renders in its thread pool, so one shared parser serves concurrent requests."""
    docs = [f"# {n}\n\n| a |\n|---|\n| [{n}](https://e.example/{n}) |\n" * 40 for n in range(8)]
    expected = [render("a.md", doc.encode()).body for doc in docs]
    with ThreadPoolExecutor(max_workers=8) as pool:
        got = list(pool.map(lambda n: render("a.md", docs[n % 8].encode()).body, range(200)))
    assert got == [expected[n % 8] for n in range(200)]


def test_the_agent_is_told_the_rules_of_every_type_the_api_renders() -> None:
    """A renderer nobody told the agent about serves pages written to the wrong rules."""
    assert set(RENDERERS) == set(PAGE_RULES)


def _padded_tables(count: int) -> bytes:
    """Tables whose one-cell rows markdown-it pads to a 181-column header: under 1.5 KB of
    source, and about 200,000 tokens, each."""
    table = "|" + "a|" * 181 + "\n" + "|" + "-|" * 181 + "\n" + "a\n" * 362 + "\n"
    return (table * count).encode()


@pytest.mark.parametrize(
    "markdown",
    [_padded_tables(2), b"- a\n" * 60_000, b"*_" * 150_000],
    ids=["padded-tables", "long-list", "one-huge-paragraph"],
)
def test_a_markdown_page_that_would_render_too_large_is_refused(markdown: bytes) -> None:
    """A few kilobytes must never cost the API gigabytes to answer."""
    with pytest.raises(RenderTooLarge):
        render("summary.md", markdown)


def test_a_dense_page_past_the_skills_200_kb_still_renders() -> None:
    page = "".join(
        f"## S{n}\n\nSome text with a [link](https://x.example/{n}) and **bold**.\n\n"
        f"| a | b |\n|---|---|\n| {n} | 2 |\n\n"
        for n in range(2200)
    )
    assert len(page) > 200_000
    assert _main(page).count("<table>") == 2200
