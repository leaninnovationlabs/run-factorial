"""Images embedded in GitLab issue and merge-request text.

GitHub's signed-URL dance does not apply: GitLab serves `/uploads/` and other images to a
PRIVATE-TOKEN. The worker still decides WHICH images a run may receive (`worker/attachments.py`);
this module fetches what it is told. An issue's author names these URLs, so each one, and each
redirect, must pass the address rules a project's GitLab URL does (`hosts.check_fetch_url`)
before anything connects: otherwise the worker requests addresses inside the deployment.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import Final
from urllib.parse import urljoin

import httpx

from orchestrator.github.media import (
    NOT_AN_IMAGE,
    UNEXPECTED_REDIRECT,
    Fetched,
    ItemRef,
    MediaLimits,
    MediaRef,
    MediaTransient,
    Unavailable,
    bare,
    sniff,
)
from orchestrator.gitlab.client import GitLabClient
from orchestrator.gitlab.hosts import HostLookupFailed, HostRefused, check_fetch_url
from orchestrator.gitlab.urls import gitlab_origin
from orchestrator.settings import normalized_origin

__all__ = ["GitLabMedia", "gitlab_media_refs"]

_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]{0,1024}\]\(\s*<?([^)\s>]{1,4096})")
_HTML_IMAGE = re.compile(
    r"<img\b[^>]{0,1024}?\bsrc\s*=\s*[\"']([^\"'\s]{1,4096})[\"']", re.IGNORECASE
)
_MAX_REDIRECTS: Final = 5
_REFUSED_ADDRESS: Final = "not accessible"
_UPLOAD = re.compile(r"(?:https?://[^/\s]+)?(/uploads/[^\s)\"'<>\]]+)", re.IGNORECASE)


def gitlab_media_refs(text: str) -> tuple[MediaRef, ...]:
    """Markdown and HTML image links, including GitLab's relative `/uploads/` paths."""
    found: list[tuple[int, MediaRef]] = []
    for pattern in (_MARKDOWN_IMAGE, _HTML_IMAGE):
        for match in pattern.finditer(text):
            url = match.group(1).strip()
            if not url:
                continue
            upload = _UPLOAD.search(url)
            if upload:
                origin = upload.group(1)
                found.append((match.start(1), MediaRef("asset", origin, origin)))
                continue
            if url.startswith(("http://", "https://")):
                found.append((match.start(1), MediaRef("external", bare(url), url)))
    refs: list[MediaRef] = []
    seen: set[str] = set()
    for _, ref in sorted(found, key=lambda pair: pair[0]):
        if ref.asset not in seen:
            seen.add(ref.asset)
            refs.append(ref)
    return tuple(refs)


class GitLabMedia:
    """`MediaSource` against one project's token."""

    def __init__(
        self,
        client: GitLabClient,
        repo: str,
        *,
        limits: MediaLimits | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = client
        self._repo = repo
        self._limits = limits or MediaLimits()
        self._transport = transport

    async def resolve(
        self, wanted: Sequence[tuple[ItemRef, MediaRef]]
    ) -> tuple[Fetched | Unavailable | None, ...]:
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                follow_redirects=False,
                timeout=httpx.Timeout(15.0, connect=5.0),
            ) as http:
                return await self._resolve(http, wanted)
        except TimeoutError as exc:
            raise MediaTransient("GitLab image fetch overran its budget") from exc

    async def _resolve(
        self, http: httpx.AsyncClient, wanted: Sequence[tuple[ItemRef, MediaRef]]
    ) -> tuple[Fetched | Unavailable | None, ...]:
        out: list[Fetched | Unavailable | None] = []
        count = 0
        total = 0
        token = self._client.settings.token.get_secret_value()
        origin = gitlab_origin(
            {"gitlab": {"base_url": self._client.settings.base_url}},
            fallback=self._client.settings.base_url,
        )
        for _item, ref in wanted:
            if count >= self._limits.images:
                out.append(Unavailable(ref, f"over the {self._limits.images}-image limit"))
                continue
            outcome = await self._download(http, ref, origin, token)
            if isinstance(outcome, Fetched):
                if total + len(outcome.content) > self._limits.run_bytes:
                    outcome = Unavailable(
                        ref, f"over the {self._limits.run_bytes // (1024 * 1024)} MB run limit"
                    )
                else:
                    count += 1
                    total += len(outcome.content)
            out.append(outcome)
        return tuple(out)

    async def _download(
        self, http: httpx.AsyncClient, ref: MediaRef, origin: str, token: str
    ) -> Fetched | Unavailable:
        target = ref.origin
        if target.startswith("/"):
            target = f"{origin}/{self._repo}{target}"
        own = normalized_origin(origin)
        # Redirects are followed here, not by httpx, which would carry PRIVATE-TOKEN to any host
        # a redirect names.
        for _hop in range(_MAX_REDIRECTS + 1):
            same = own is not None and normalized_origin(target) == own
            headers = {"PRIVATE-TOKEN": token} if same else {}
            try:
                await check_fetch_url(target)
            except HostRefused:
                return Unavailable(ref, _REFUSED_ADDRESS)
            except HostLookupFailed as exc:
                raise MediaTransient(f"looking up {bare(target)} failed: {exc}") from None
            try:
                response = await http.get(target, headers=headers)
            except httpx.TransportError as exc:
                raise MediaTransient(
                    f"downloading {bare(target)} failed: {type(exc).__name__}"
                ) from None
            location = response.headers.get("location")
            if not (response.is_redirect and location):
                break
            target = urljoin(target, location)
        else:
            return Unavailable(ref, UNEXPECTED_REDIRECT)
        if response.status_code in {401, 403, 404}:
            reason = NOT_AN_IMAGE if response.status_code == 404 else "not accessible"
            return Unavailable(ref, reason)
        if response.status_code >= 400:
            raise MediaTransient(f"downloading {bare(target)} failed: HTTP {response.status_code}")
        content = response.content
        if len(content) > self._limits.image_bytes:
            return Unavailable(
                ref, f"over the {self._limits.image_bytes // (1024 * 1024)} MB image limit"
            )
        media_type = sniff(content)
        if media_type is None:
            return Unavailable(ref, NOT_AN_IMAGE)
        return Fetched(ref, media_type, content, hashlib.sha256(content).hexdigest())
