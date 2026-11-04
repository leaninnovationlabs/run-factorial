"""How the API serves a file a step declares (tech-stack decision 58), by its extension.

Every answer goes out under one inert envelope (`api/routes/pages.py`, design.md §7.15); this table
decides only the body and the media type inside it. A suffix with no entry is served raw as
`text/plain`, never as a type guessed from the extension, so a file cannot choose the document it
becomes. `runtime.adapters._cli.environment.PAGE_RULES` tells the agent the same table, and a test
holds the two to one key set.
"""

from __future__ import annotations

import html
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final, override

import markdown_it
from markdown_it import MarkdownIt
from markdown_it.common.normalize_url import validateLink
from markdown_it.parser_block import RuleFuncBlockType
from markdown_it.rules_block import StateBlock
from markdown_it.rules_core import StateCore

__all__ = [
    "RENDERERS",
    "RENDER_REVISION",
    "RENDER_TAG",
    "NotText",
    "RenderTooLarge",
    "Rendered",
    "render",
]

_HTML: Final = "text/html; charset=utf-8"
_TEXT: Final = "text/plain; charset=utf-8"

#: Past this many tokens a Markdown page is refused rather than rendered. A small source can expand
#: enormously: markdown-it pads every table row to its header's width, so 1.5 KB of wide-header
#: table is about 200,000 tokens. A dense 226 KB page is about 92,000.
_TOKEN_BUDGET: Final = 200_000

#: Bump it whenever a renderer's output changes for the same bytes, or a suffix gains or loses a
#: renderer: a browser holding the old answer would otherwise get a 304.
RENDER_REVISION: Final = 1

#: Every page's ETag carries it, so neither an edit here nor a markdown-it-py upgrade in the lock
#: can leave a browser holding an older render.
RENDER_TAG: Final = f"r{RENDER_REVISION}-{markdown_it.__version__}"


@dataclass(frozen=True)
class Rendered:
    body: bytes
    media_type: str


class NotText(ValueError):
    """The page's bytes are not UTF-8, and its type is served as text."""


class RenderTooLarge(ValueError):
    """The Markdown page would render past `_TOKEN_BUDGET` tokens."""


def _text(body: bytes) -> str:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NotText("the page is not UTF-8 text") from exc


def _as_html(body: bytes, path: str) -> Rendered:
    return Rendered(body, _HTML)


def _raw(body: bytes, path: str) -> Rendered:
    _text(body)
    return Rendered(body, _TEXT)


def _tame_links(state: StateCore) -> None:
    """Every link opens a new tab: without script nothing else stops a clicked link moving the
    frame to another site (design.md §7.15). A link to a `data:` URL is reduced to its text: the
    allowance is for images, and a tab opened from the page escapes its sandbox."""
    for token in state.tokens:
        if not token.children:
            continue
        kept = []
        dropping = False
        for child in token.children:
            if child.type == "link_open":
                if str(child.attrGet("href") or "").lower().startswith("data:"):
                    dropping = True
                    continue
                child.attrSet("target", "_blank")
                child.attrSet("rel", "noopener noreferrer")
            elif child.type == "link_close" and dropping:
                # Links never nest, so the next close is the dropped link's own.
                dropping = False
                continue
            kept.append(child)
        token.children = kept


def _within_budget(rule: RuleFuncBlockType) -> RuleFuncBlockType:
    # Every block rule, so a list's nested items are counted while it is still being built.
    def budgeted(state: StateBlock, start: int, end: int, silent: bool) -> bool:
        found = rule(state, start, end, silent)
        if len(state.tokens) > _TOKEN_BUDGET:
            raise RenderTooLarge("the page renders too large")
        return found

    return budgeted


def _inline_within_budget(state: StateCore) -> None:
    """markdown-it's core `inline` rule, refusing a paragraph before its parse could pass the
    budget: a paragraph never yields more tokens than it has characters."""
    used = len(state.tokens)
    for token in state.tokens:
        if token.type != "inline":
            continue
        if used + len(token.content) > _TOKEN_BUDGET:
            raise RenderTooLarge("the page renders too large")
        if token.children is None:
            token.children = []
        state.md.inline.parse(token.content, state.md, state.env, token.children)
        used += len(token.children)


class _PageMarkdown(MarkdownIt):
    """CommonMark with tables and strikethrough. Raw HTML is escaped, and an SVG `data:` image is
    allowed beside the raster ones markdown-it allows: an `<img>` runs no script."""

    def __init__(self) -> None:
        super().__init__("commonmark", {"html": False})
        self.enable(["table", "strikethrough"])
        for rule in self.block.ruler.__rules__:
            self.block.ruler.at(rule.name, _within_budget(rule.fn), {"alt": rule.alt})
        self.core.ruler.at("inline", _inline_within_budget)
        self.core.ruler.push("tame_links", _tame_links)
        # markdown-it builds each ruler's chains on first use, and a thread reading them while
        # another builds them can get them half built: build them before any request can race.
        for ruler in (self.core.ruler, self.block.ruler, self.inline.ruler, self.inline.ruler2):
            ruler.getRules("")

    @override
    def validateLink(self, url: str) -> bool:
        return validateLink(url) or url.lower().startswith(
            ("data:image/svg+xml;", "data:image/svg+xml,")
        )


_MARKDOWN: Final = _PageMarkdown()

_STYLE: Final = (
    ":root{color-scheme:light dark;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;"
    "--code:#f6f8fa;--link:#0969da;--bg:#fff}"
    "@media (prefers-color-scheme:dark){:root{--fg:#e6edf3;--muted:#9198a1;--line:#3d444d;"
    "--code:#151b23;--link:#4493f8;--bg:#0d1117}}"
    "body{margin:0;background:var(--bg);color:var(--fg);"
    'font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}'
    "main{max-width:72ch;margin:0 auto;padding:24px 16px 48px}"
    "h1,h2,h3,h4{line-height:1.25;margin:1.5em 0 .5em}"
    "a{color:var(--link)}img{max-width:100%}"
    "code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;"
    "font-size:.875em;background:var(--code);border-radius:4px}"
    "code{padding:.1em .3em}pre{padding:12px 16px;overflow-x:auto}"
    "pre code{padding:0;background:none;font-size:1em}"
    "table{border-collapse:collapse;display:block;overflow-x:auto}"
    "th,td{border:1px solid var(--line);padding:6px 12px;text-align:left}"
    "blockquote{margin:0;padding-left:1em;border-left:3px solid var(--line);color:var(--muted)}"
    "hr{border:0;border-top:1px solid var(--line)}"
)


def _markdown(body: bytes, path: str) -> Rendered:
    # An editor's byte-order mark would keep the first line from parsing as a heading.
    text = _text(body).removeprefix("\ufeff")
    page = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{html.escape(path)}</title><style>{_STYLE}</style></head>"
        f"<body><main>{_MARKDOWN.render(text)}</main></body></html>"
    )
    return Rendered(page.encode(), _HTML)


Renderer = Callable[[bytes, str], Rendered]

#: Keyed by the lower-cased suffix of the path the worker observed.
RENDERERS: Final[Mapping[str, Renderer]] = {".html": _as_html, ".md": _markdown}


def render(path: str, body: bytes) -> Rendered:
    """`body`, a page found at `path`, as the API answers it. Raises `NotText` for a type served
    as text whose bytes are not UTF-8, and `RenderTooLarge` for a Markdown page past the budget."""
    return RENDERERS.get(PurePosixPath(path).suffix.lower(), _raw)(body, path)
