"""Images embedded in issue and pull-request text, fetched for an agent run (design.md §7.11).

The one module that knows GitHub's media URL shapes. An App installation token cannot fetch
`github.com/user-attachments/assets/<uuid>` — GitHub answers 404, as it does an anonymous
request — so the path is the item's rendered `body_html`. Fetched with
`Accept: application/vnd.github.full+json`, it rewrites each image the item's own repository owns
to a `private-user-images.githubusercontent.com/...?jwt=` URL: a five-minute bearer credential
that downloads with no Authorization header at all. GitHub signs only assets of the repository
the item is rendered in, whatever the token can reach, so nothing here can pull another
repository's image into a run.

Trust is not decided here (`state.py`'s neutrality): the worker names the items whose text an
agent receives, and this module fetches what it is told.

Signed URLs never leave this module. `SignedUrl` prints query-stripped, every exception message
is built from `bare()`, and the download client never carries a credential — a forwarded
Authorization header is echoed back by S3 in its error body.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Any, Final, Literal
from urllib.parse import urlsplit

import httpx
from githubkit import Response

from orchestrator.github.client import GitHubClient, split_repo
from orchestrator.github.observer import PER_PAGE, raw_pages

__all__ = [
    "NOT_ACCESSIBLE",
    "NOT_AN_IMAGE",
    "REMOVED",
    "UNEXPECTED_REDIRECT",
    "Embedded",
    "Fetched",
    "GitHubMedia",
    "ItemKind",
    "ItemRef",
    "MediaLimits",
    "MediaRef",
    "MediaTransient",
    "MediaType",
    "RefKind",
    "SignedUrl",
    "Unavailable",
    "bare",
    "embedded_media",
    "media_refs",
    "sniff",
]

MediaType = Literal["image/png", "image/jpeg", "image/gif", "image/webp"]
ItemKind = Literal["issue", "issue_comment", "review", "review_comment"]
#: `asset`: an upload GitHub hosts. `file`: a non-image upload, which GitHub renders as a link and
#: never signs. `external`: any other image an author embeds by URL, served through camo.
RefKind = Literal["asset", "file", "external"]

_UUID: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_UUID_RE: Final = re.compile(_UUID, re.IGNORECASE)
#: Today's `user-attachments/assets/<uuid>`, and the 2023-24 `<owner>/<repo>/assets/<uid>/<uuid>`.
_GITHUB_ASSET: Final = re.compile(
    rf"https://github\.com/(?:user-attachments/assets|[\w.-]+/[\w.-]+/assets/\d+)/{_UUID}",
    re.IGNORECASE,
)
_GITHUB_FILE: Final = re.compile(
    r"https://github\.com/user-attachments/files/\d+/[^\s)\"'<>\]]+", re.IGNORECASE
)
#: Pre-2023 uploads, public by URL, and signed links pasted as they are.
_USER_IMAGES: Final = re.compile(
    r"https://(?:private-)?user-images\.githubusercontent\.com/[^\s)\"'<>\]]+", re.IGNORECASE
)
_MARKDOWN_IMAGE: Final = re.compile(r"!\[[^\]]{0,1024}\]\(\s*<?(https?://[^\s)>]{1,4096})")
_HTML_IMAGE: Final = re.compile(
    r"<img\b[^>]{0,1024}?\bsrc\s*=\s*[\"']([^\"'\s]{1,4096})[\"']", re.IGNORECASE
)

SIGNED_HOST: Final = "private-user-images.githubusercontent.com"
LEGACY_HOST: Final = "user-images.githubusercontent.com"
CAMO_HOST: Final = "camo.githubusercontent.com"


def bare(url: str) -> str:
    """`url` without its query or fragment — the only form this module ever prints."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}{parts.path}"


def _asset_key(url: str) -> str:
    """What joins a link in text to its rendering: an upload's uuid, else the URL itself."""
    uuids = _UUID_RE.findall(urlsplit(url).path)
    return uuids[-1].lower() if uuids else url


def _is_github(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return host == "github.com" or host.endswith(".githubusercontent.com")


@dataclass(frozen=True, slots=True)
class MediaRef:
    """One media link as written in text. `origin` carries no query string, so no signature."""

    kind: RefKind
    origin: str
    asset: str


@dataclass(frozen=True, slots=True)
class ItemRef:
    """One observed item: the issue itself (`id` None), or a comment, review or inline comment on
    issue or PR `number`."""

    kind: ItemKind
    number: int
    id: int | None = None


class SignedUrl:
    """A download URL that may be a bearer credential. Only `reveal()` returns it whole."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"SignedUrl({bare(self._value)!r})"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SignedUrl) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)


@dataclass(frozen=True, slots=True)
class Embedded:
    """One image or video GitHub rendered into an item. `url` is None when GitHub left the asset
    unsigned: this repository cannot read it."""

    asset: str
    tag: Literal["img", "video"]
    url: SignedUrl | None


def media_refs(text: str) -> tuple[MediaRef, ...]:
    """Every media link in `text`, in order, once each — the cheap gate that decides whether a
    run pays for any `body_html` call at all. A match is only a candidate: a link inside a code
    span matches here and is embedded nowhere, and `body_html` decides."""
    found: list[tuple[int, MediaRef]] = []
    for match in _GITHUB_ASSET.finditer(text):
        url = match.group(0)
        found.append((match.start(), MediaRef("asset", bare(url), _asset_key(url))))
    for match in _GITHUB_FILE.finditer(text):
        url = bare(match.group(0))
        found.append((match.start(), MediaRef("file", url, url)))
    for match in _USER_IMAGES.finditer(text):
        url = unescape(match.group(0))
        found.append((match.start(), MediaRef("asset", bare(url), _asset_key(url))))
    for pattern in (_MARKDOWN_IMAGE, _HTML_IMAGE):
        for match in pattern.finditer(text):
            url = unescape(match.group(1))
            try:
                github = _is_github(url)
            except ValueError:  # `urlsplit` refuses a malformed host, and an author can type one
                continue
            if not github:
                found.append((match.start(1), MediaRef("external", bare(url), _asset_key(url))))
    refs: list[MediaRef] = []
    seen: set[str] = set()
    for _, ref in sorted(found, key=lambda pair: pair[0]):
        if ref.asset not in seen:
            seen.add(ref.asset)
            refs.append(ref)
    return tuple(refs)


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        src = values.get("src")
        if tag in ("img", "video") and src:
            self.found.append((tag, src, values.get("data-canonical-src")))


def embedded_media(body_html: str) -> dict[str, Embedded]:
    """The images and videos GitHub rendered into one item, by asset key."""
    parser = _Tags()
    parser.feed(body_html)
    parser.close()
    out: dict[str, Embedded] = {}
    for tag, src, canonical in parser.found:
        parts = urlsplit(src)
        host = parts.hostname or ""
        if host == CAMO_HOST:
            if canonical is None:
                continue
            try:
                key = _asset_key(canonical)
            except ValueError:  # GitHub echoes an author's malformed URL here as written
                continue
            url = SignedUrl(src)
        elif host == SIGNED_HOST:
            key = _asset_key(src)
            url = SignedUrl(src) if "jwt=" in parts.query else None
        elif host == LEGACY_HOST:
            key, url = _asset_key(src), SignedUrl(src)
        elif host == "github.com":
            key, url = _asset_key(src), None
        else:
            continue
        out.setdefault(key, Embedded(key, "video" if tag == "video" else "img", url))
    return out


def sniff(content: bytes) -> MediaType | None:
    """The image type by magic bytes — authoritative over any Content-Type header."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


#: Hosts a download may reach. Only ever taken from `body_html`, which GitHub rendered — never
#: from a URL an author typed.
_DOWNLOAD_HOSTS: Final = frozenset({SIGNED_HOST, LEGACY_HOST, CAMO_HOST})
_FULL_JSON: Final = {"Accept": "application/vnd.github.full+json"}
_MB: Final = 1024 * 1024


class _RedactSignedUrls(logging.Filter):
    """httpx logs every request URL at INFO, and a signed URL is a bearer credential."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self._bare(arg) for arg in record.args)
        return True

    @staticmethod
    def _bare(arg: object) -> object:
        if isinstance(arg, httpx.URL | str):
            try:
                host = urlsplit(str(arg)).hostname
            except ValueError:
                return arg
            if host in _DOWNLOAD_HOSTS:
                return bare(str(arg))
        return arg


logging.getLogger("httpx").addFilter(_RedactSignedUrls())

#: `Unavailable.reason`: what the prompt tells the agent about an image it does not get.
NOT_AN_IMAGE: Final = "not an image"
NOT_ACCESSIBLE: Final = "not accessible from this repository"
REMOVED: Final = "removed or expired"
UNEXPECTED_REDIRECT: Final = "unexpected redirect"


@dataclass(frozen=True, slots=True)
class Fetched:
    ref: MediaRef
    media_type: MediaType
    content: bytes = field(repr=False)
    sha256: str


@dataclass(frozen=True, slots=True)
class Unavailable:
    ref: MediaRef
    reason: str


class MediaTransient(Exception):
    """GitHub could not serve an image right now.

    Not a `RuntimeError`, so the tick rolls back and the spawn is retried rather than escalated
    (`worker/failures.py`'s `_PERMANENT_CONFIG_ERRORS`). The message is built from `bare()` URLs
    only: after enough ticks it becomes a quarantine's recorded error.
    """


class _Expired(Exception):
    """A signed URL GitHub no longer honours: re-sign from a fresh `body_html` and try again."""


@dataclass(frozen=True, slots=True)
class MediaLimits:
    #: Above 20 images in one request the Anthropic API caps each at 2000 px.
    images: int = 20
    #: Under the direct API's 10 MB base64 limit. The claude CLI re-encodes a large image itself;
    #: codex does not, so this is what bounds its request.
    image_bytes: int = 5 * _MB
    #: The Anthropic API's 32 MB request cap, counted before base64 adds a third: the claude CLI
    #: re-encodes a large image, and OpenAI's cap is 512 MB. Also a bound on stdin and memory.
    run_bytes: int = 32 * _MB
    #: Download tries per image; each after the first re-signs from a fresh `body_html`.
    attempts: int = 3
    #: The whole resolution, well inside a ticket's lease (`settings.lease_seconds`).
    budget_seconds: float = 60.0


class _Bodies:
    """`body_html` per (kind, number), fetched once per round; `fresh` re-fetches, which is what
    renews every signature in it."""

    def __init__(self, fetch: Callable[[ItemKind, int], Awaitable[dict[int | None, str]]]) -> None:
        self._fetch = fetch
        self._cache: dict[tuple[ItemKind, int], dict[int | None, str]] = {}

    async def embedded(self, item: ItemRef, *, fresh: bool) -> dict[str, Embedded]:
        key = (item.kind, item.number)
        if fresh or key not in self._cache:
            self._cache[key] = await self._fetch(item.kind, item.number)
        return embedded_media(self._cache[key].get(item.id, ""))


class GitHubMedia:
    """`worker.attachments.MediaSource` over one GitHub repository."""

    def __init__(
        self,
        client: GitHubClient,
        repo: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        limits: MediaLimits | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._repo = repo
        self._owner, self._name = split_repo(repo)
        self._transport = transport
        self._limits = limits if limits is not None else MediaLimits()
        self._sleep = sleep

    async def resolve(
        self, wanted: Sequence[tuple[ItemRef, MediaRef]]
    ) -> tuple[Fetched | Unavailable | None, ...]:
        """One outcome per `(item, ref)`, in order. None: the text names it but GitHub embedded
        nothing there — a code span or a plain link, text rather than an image.

        Raises `MediaTransient` when GitHub cannot serve an image after `limits.attempts` tries,
        or the whole resolution overruns `limits.budget_seconds`.
        """
        try:
            async with (
                asyncio.timeout(self._limits.budget_seconds),
                httpx.AsyncClient(
                    transport=self._transport,
                    follow_redirects=False,
                    timeout=httpx.Timeout(15.0, connect=5.0),
                ) as http,
            ):
                return await self._resolve(http, wanted)
        except TimeoutError:
            raise MediaTransient(
                f"resolving images for {self._repo} took over {self._limits.budget_seconds:g} s"
            ) from None

    async def _resolve(
        self, http: httpx.AsyncClient, wanted: Sequence[tuple[ItemRef, MediaRef]]
    ) -> tuple[Fetched | Unavailable | None, ...]:
        bodies = _Bodies(self._fetch_bodies)
        out: list[Fetched | Unavailable | None] = []
        count = total = 0
        for item, ref in wanted:
            if ref.kind == "file":
                out.append(Unavailable(ref, NOT_AN_IMAGE))
                continue
            outcome = await self._one(http, bodies, item, ref, capped=count >= self._limits.images)
            if isinstance(outcome, Fetched):
                if total + len(outcome.content) > self._limits.run_bytes:
                    outcome = Unavailable(
                        ref, f"over the {self._limits.run_bytes // _MB} MB run limit"
                    )
                else:
                    count += 1
                    total += len(outcome.content)
            out.append(outcome)
        return tuple(out)

    async def _one(
        self,
        http: httpx.AsyncClient,
        bodies: _Bodies,
        item: ItemRef,
        ref: MediaRef,
        *,
        capped: bool,
    ) -> Fetched | Unavailable | None:
        failure: MediaTransient | None = None
        for attempt in range(self._limits.attempts):
            embedded = (await bodies.embedded(item, fresh=attempt > 0)).get(ref.asset)
            if embedded is None:
                return None
            if embedded.url is None:
                return Unavailable(ref, NOT_ACCESSIBLE)
            if embedded.tag == "video":
                return Unavailable(ref, NOT_AN_IMAGE)
            if capped:
                return Unavailable(ref, f"over the {self._limits.images}-image limit")
            try:
                return await self._download(http, ref, embedded.url)
            except _Expired:
                failure = None
            except MediaTransient as exc:
                failure = exc
                if attempt + 1 < self._limits.attempts:
                    await self._sleep(0.5 * 2**attempt)
        if failure is not None:
            raise failure
        return Unavailable(ref, REMOVED)

    async def _download(
        self, http: httpx.AsyncClient, ref: MediaRef, url: SignedUrl
    ) -> Fetched | Unavailable:
        target = url.reveal()
        parts = urlsplit(target)
        if parts.scheme != "https" or parts.hostname not in _DOWNLOAD_HOSTS:
            return Unavailable(ref, NOT_ACCESSIBLE)
        too_large = Unavailable(ref, f"larger than {self._limits.image_bytes // _MB} MB")
        chunks: list[bytes] = []
        try:
            async with http.stream("GET", target) as response:
                status = response.status_code
                if 300 <= status < 400:
                    return Unavailable(ref, UNEXPECTED_REDIRECT)
                if status == 404:
                    raise _Expired
                if status == 429 or status >= 500:
                    raise MediaTransient(f"{bare(target)} answered {status}")
                if status != 200:
                    return Unavailable(ref, f"GitHub answered {status}")
                if not response.headers.get("content-type", "").lower().startswith("image/"):
                    return Unavailable(ref, NOT_AN_IMAGE)
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > self._limits.image_bytes:
                    return too_large
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self._limits.image_bytes:
                        return too_large
                    chunks.append(chunk)
        except httpx.TransportError as exc:
            raise MediaTransient(
                f"downloading {bare(target)} failed: {type(exc).__name__}"
            ) from None
        content = b"".join(chunks)
        media_type = sniff(content)
        if media_type is None:
            return Unavailable(ref, NOT_AN_IMAGE)
        return Fetched(ref, media_type, content, hashlib.sha256(content).hexdigest())

    async def _fetch_bodies(self, kind: ItemKind, number: int) -> dict[int | None, str]:
        """Rendered bodies of one kind of item on issue or PR `number`, by id (None: the issue
        itself). `full+json` adds `body_html` beside `body` on every one of these routes."""
        api = await self._client.api(self._repo)
        if kind == "issue":
            response = await api.rest.issues.async_get(
                self._owner, self._name, number, headers=_FULL_JSON
            )
            return {None: json.loads(response.content).get("body_html") or ""}

        async def fetch(page: int) -> Response[Any]:
            if kind == "issue_comment":
                return await api.rest.issues.async_list_comments(
                    self._owner,
                    self._name,
                    number,
                    per_page=PER_PAGE,
                    page=page,
                    headers=_FULL_JSON,
                )
            if kind == "review":
                return await api.rest.pulls.async_list_reviews(
                    self._owner,
                    self._name,
                    number,
                    per_page=PER_PAGE,
                    page=page,
                    headers=_FULL_JSON,
                )
            return await api.rest.pulls.async_list_review_comments(
                self._owner,
                self._name,
                number,
                per_page=PER_PAGE,
                page=page,
                headers=_FULL_JSON,
            )

        out: dict[int | None, str] = {}
        async for batch in raw_pages(fetch):
            out.update(
                {
                    row["id"]: row.get("body_html") or ""
                    for row in batch
                    if isinstance(row.get("id"), int)
                }
            )
        return out
