"""GitHub media: which links a text names, what GitHub rendered for them, and what a download
may return. No network: rendered bodies are literals, downloads go through `httpx.MockTransport`."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace

import httpx
import pytest

from orchestrator.github.client import GitHubClient
from orchestrator.github.media import (
    NOT_ACCESSIBLE,
    NOT_AN_IMAGE,
    REMOVED,
    UNEXPECTED_REDIRECT,
    Fetched,
    GitHubMedia,
    ItemRef,
    MediaLimits,
    MediaRef,
    MediaTransient,
    SignedUrl,
    Unavailable,
    bare,
    embedded_media,
    media_refs,
    sniff,
)

UUID = "1b92ad15-13e5-4ffe-ac3a-b0c19487acfa"
ASSET = f"https://github.com/user-attachments/assets/{UUID}"
SIGNED = (
    f"https://private-user-images.githubusercontent.com/244545209/654085438-{UUID}.png"
    "?jwt=eyJhbGciOiJIUzI1NiJ9.e30.sig&amp;x=1"
)

# --------------------------------------------------------------------------- media_refs


def test_the_html_img_github_writes_on_paste_is_found() -> None:
    body = f'Extract the text.\n\n<img width="278" height="98" alt="Image" src="{ASSET}" />'
    assert media_refs(body) == (MediaRef("asset", ASSET, UUID),)


def test_markdown_images_and_the_older_repo_assets_form_are_found_in_text_order() -> None:
    older = "https://github.com/acme/widgets/assets/142835398/e9a940d4-2d9f-4782-816c-b46cdf2546ce"
    text = f"first ![a]({older}) then ![b]({ASSET})"
    assert [ref.asset for ref in media_refs(text)] == [
        "e9a940d4-2d9f-4782-816c-b46cdf2546ce",
        UUID,
    ]


def test_an_uppercase_uuid_joins_like_a_lowercase_one() -> None:
    (ref,) = media_refs(ASSET.upper().replace("HTTPS://GITHUB.COM", "https://github.com"))
    assert ref.asset == UUID


def test_a_link_named_twice_is_one_ref() -> None:
    assert len(media_refs(f"{ASSET}\n\n![again]({ASSET})")) == 1


def test_a_legacy_upload_is_an_asset_ref() -> None:
    legacy = (
        "https://user-images.githubusercontent.com/1/125086838-0e2c1a6f-1234-4abc-9def-"
        "0123456789ab.png"
    )
    (ref,) = media_refs(f"![x]({legacy})")
    assert ref.kind == "asset"
    assert ref.asset == "0e2c1a6f-1234-4abc-9def-0123456789ab"


def test_a_pasted_signed_link_keeps_no_query_in_its_origin() -> None:
    (ref,) = media_refs(f"![x]({SIGNED.replace('&amp;', '&')})")
    assert "?" not in ref.origin
    assert ref.asset == UUID


def test_a_file_upload_is_a_file_ref() -> None:
    (ref,) = media_refs("see https://github.com/user-attachments/files/123/spec.pdf")
    assert ref.kind == "file"
    assert ref.origin == "https://github.com/user-attachments/files/123/spec.pdf"


def test_an_external_image_is_an_external_ref_keyed_by_its_url() -> None:
    (ref,) = media_refs("![chart](https://i.imgur.com/abc.png?v=2)")
    assert ref == MediaRef("external", "https://i.imgur.com/abc.png", "https://i.imgur.com/abc.png?v=2")


def test_an_external_images_userinfo_never_reaches_its_origin() -> None:
    (ref,) = media_refs("![x](https://user:pw@i.example.com/a.png)")
    assert "pw" not in ref.origin
    assert "@" not in ref.origin


def test_an_image_url_with_whitespace_is_not_an_image_ref() -> None:
    assert media_refs('<img src="https://example.com/a b.png">') == ()


def test_an_image_url_with_an_impossible_port_is_an_external_ref() -> None:
    assert media_refs("![x](https://h:99999/a.png)") == (
        MediaRef("external", "https://h:99999/a.png", "https://h:99999/a.png"),
    )


def test_an_image_url_with_a_host_urlsplit_refuses_is_skipped() -> None:
    assert media_refs(f'<img src="https://[/a.png"> ![a]({ASSET})') == (
        MediaRef("asset", ASSET, UUID),
    )


def test_a_repo_file_image_is_out_of_scope() -> None:
    assert media_refs("![x](https://github.com/acme/widgets/blob/main/x.png?raw=true)") == ()


def test_text_without_media_names_nothing() -> None:
    assert media_refs("please add a widget") == ()


# --------------------------------------------------------------------------- embedded_media


def test_a_signed_image_is_embedded_with_its_url_unescaped() -> None:
    html = f'<p><a href="{SIGNED}"><img src="{SIGNED}" alt="Image" /></a></p>'
    embedded = embedded_media(html)[UUID]
    assert embedded.tag == "img"
    assert embedded.url is not None
    assert "&amp;" not in embedded.url.reveal()
    assert "jwt=" in embedded.url.reveal()


def test_an_asset_github_left_unsigned_has_no_url() -> None:
    assert embedded_media(f'<img src="{ASSET}">')[UUID].url is None


def test_a_private_images_url_without_a_signature_has_no_url() -> None:
    unsigned = f"https://private-user-images.githubusercontent.com/1/2-{UUID}.png"
    assert embedded_media(f'<img src="{unsigned}">')[UUID].url is None


def test_an_external_image_is_embedded_through_camo_by_its_canonical_url() -> None:
    html = (
        '<img src="https://camo.githubusercontent.com/abc/68747470" '
        'data-canonical-src="https://i.imgur.com/abc.png?v=2">'
    )
    embedded = embedded_media(html)["https://i.imgur.com/abc.png?v=2"]
    assert embedded.url is not None
    assert embedded.url.reveal().startswith("https://camo.githubusercontent.com/")


def test_a_camo_image_whose_canonical_url_urlsplit_refuses_is_skipped() -> None:
    html = (
        '<img src="https://camo.githubusercontent.com/7a2e/68747470733a2f2f5b2f612e706e67" '
        'data-canonical-src="https://[/a.png">'
        f'<img src="{SIGNED}">'
    )
    assert embedded_media(html).keys() == {UUID}


def test_a_video_is_embedded_as_a_video() -> None:
    assert embedded_media(f'<video src="{SIGNED}"></video>')[UUID].tag == "video"


def test_a_code_span_embeds_nothing() -> None:
    assert embedded_media(f"<p><code>{ASSET}</code></p>") == {}


# --------------------------------------------------------------------------- sniff / redaction


def test_each_supported_type_is_known_by_its_magic_bytes() -> None:
    assert sniff(b"\x89PNG\r\n\x1a\n...") == "image/png"
    assert sniff(b"\xff\xd8\xff\xe0...") == "image/jpeg"
    assert sniff(b"GIF89a...") == "image/gif"
    assert sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"


def test_anything_else_is_not_an_image() -> None:
    assert sniff(b"<svg xmlns='http://www.w3.org/2000/svg'/>") is None
    assert sniff(b"<!DOCTYPE html>") is None
    assert sniff(b"") is None


def test_a_signed_url_prints_without_its_signature() -> None:
    url = SignedUrl(SIGNED.replace("&amp;", "&"))
    assert "jwt" not in repr(url)
    assert "jwt" not in str(url)
    assert "jwt" not in f"{url}"


PNG = b"\x89PNG\r\n\x1a\n" + b"a-real-enough-png"
ISSUE = ItemRef("issue", 145)
SIGNED_RAW = SIGNED.replace("&amp;", "&")
(REF,) = media_refs(f'<img src="{ASSET}">')


def _signed(n: int) -> str:
    """The n-th signature GitHub hands out for the same asset."""
    return f"https://private-user-images.githubusercontent.com/244545209/654085438-{UUID}.png?jwt=sig{n}"


class _Bodies:
    """Stands in for `GitHubMedia._fetch_bodies`: each call renders the next signature."""

    def __init__(self, render: Callable[[int], str] | None = None) -> None:
        self.calls = 0
        self._render = render or (lambda n: f'<img src="{_signed(n)}">')

    async def __call__(self, kind: str, number: int) -> dict[int | None, str]:
        self.calls += 1
        return {None: self._render(self.calls)}


def _media(
    handler: Callable[[httpx.Request], object],
    bodies: _Bodies | None = None,
    **limits: object,
) -> tuple[GitHubMedia, list[httpx.Request], _Bodies, list[float]]:
    requests: list[httpx.Request] = []
    slept: list[float] = []

    async def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        result = handler(request)
        if asyncio.iscoroutine(result):
            result = await result
        assert isinstance(result, httpx.Response)
        return result

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    media = GitHubMedia(
        GitHubClient(None),
        "acme/widgets",
        transport=httpx.MockTransport(record),
        limits=MediaLimits(**limits),  # type: ignore[arg-type]
        sleep=sleep,
    )
    rendered = bodies or _Bodies()
    media._fetch_bodies = rendered  # type: ignore[method-assign]
    return media, requests, rendered, slept


def _png(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)


async def test_a_signed_image_downloads_with_no_credential() -> None:
    media, requests, _, _ = _media(_png)
    (outcome,) = await media.resolve([(ISSUE, REF)])

    assert outcome == Fetched(REF, "image/png", PNG, hashlib.sha256(PNG).hexdigest())
    (request,) = requests
    assert "authorization" not in request.headers
    assert request.url.host == "private-user-images.githubusercontent.com"


async def test_an_image_this_repository_cannot_read_is_named_not_fetched() -> None:
    media, requests, _, _ = _media(_png, _Bodies(lambda _: f'<img src="{ASSET}">'))
    assert await media.resolve([(ISSUE, REF)]) == (Unavailable(REF, NOT_ACCESSIBLE),)
    assert requests == []


async def test_a_link_github_did_not_embed_is_text_not_an_image() -> None:
    media, requests, _, _ = _media(_png, _Bodies(lambda _: f"<p><code>{ASSET}</code></p>"))
    assert await media.resolve([(ISSUE, REF)]) == (None,)
    assert requests == []


async def test_an_external_image_github_did_not_route_through_camo_is_not_fetched() -> None:
    url = "https://evil.example/x.png"
    (ref,) = media_refs(f"![x]({url})")
    media, requests, _, _ = _media(_png, _Bodies(lambda _: f'<img src="{url}">'))
    assert await media.resolve([(ISSUE, ref)]) == (None,)
    assert requests == []


async def test_a_camo_image_over_plain_http_is_not_fetched() -> None:
    url = "https://i.imgur.com/abc.png"
    (ref,) = media_refs(f"![x]({url})")
    html = f'<img src="http://camo.githubusercontent.com/abc/68747470" data-canonical-src="{url}">'
    media, requests, _, _ = _media(_png, _Bodies(lambda _: html))
    assert await media.resolve([(ISSUE, ref)]) == (Unavailable(ref, NOT_ACCESSIBLE),)
    assert requests == []


async def test_a_file_upload_is_named_without_any_fetch() -> None:
    (file_ref,) = media_refs("https://github.com/user-attachments/files/123/spec.pdf")
    media, requests, bodies, _ = _media(_png)
    assert await media.resolve([(ISSUE, file_ref)]) == (Unavailable(file_ref, NOT_AN_IMAGE),)
    assert requests == []
    assert bodies.calls == 0


async def test_a_video_is_named_as_not_an_image() -> None:
    media, _, _, _ = _media(_png, _Bodies(lambda n: f'<video src="{_signed(n)}"></video>'))
    assert await media.resolve([(ISSUE, REF)]) == (Unavailable(REF, NOT_AN_IMAGE),)


async def test_an_html_page_served_with_200_is_not_an_image() -> None:
    media, _, _, _ = _media(
        lambda _: httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>")
    )
    assert await media.resolve([(ISSUE, REF)]) == (Unavailable(REF, NOT_AN_IMAGE),)


async def test_an_svg_is_not_an_image() -> None:
    media, _, _, _ = _media(
        lambda _: httpx.Response(
            200, headers={"content-type": "image/svg+xml"}, content=b"<svg xmlns='x'/>"
        )
    )
    assert await media.resolve([(ISSUE, REF)]) == (Unavailable(REF, NOT_AN_IMAGE),)


async def test_an_image_over_the_byte_cap_is_refused_by_its_declared_length() -> None:
    media, _, _, _ = _media(
        lambda _: httpx.Response(
            200,
            headers={"content-type": "image/png", "content-length": str(10 * 1024 * 1024)},
            content=PNG,
        ),
        image_bytes=len(PNG) + 10,
    )
    (outcome,) = await media.resolve([(ISSUE, REF)])
    assert isinstance(outcome, Unavailable)
    assert outcome.reason.startswith("larger than ")


async def test_an_image_over_the_byte_cap_is_refused_while_it_streams() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield PNG
        yield b"x" * 64

    media, _, _, _ = _media(
        lambda _: httpx.Response(200, headers={"content-type": "image/png"}, content=chunks()),
        image_bytes=len(PNG) + 10,
    )
    (outcome,) = await media.resolve([(ISSUE, REF)])
    assert isinstance(outcome, Unavailable)
    assert outcome.reason.startswith("larger than ")


async def test_a_redirect_is_not_followed() -> None:
    media, requests, _, _ = _media(
        lambda _: httpx.Response(302, headers={"location": "https://elsewhere.example/x.png"})
    )
    assert await media.resolve([(ISSUE, REF)]) == (Unavailable(REF, UNEXPECTED_REDIRECT),)
    assert len(requests) == 1


async def test_an_expired_signature_is_renewed_once_from_a_fresh_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "sig1" in str(request.url):
            return httpx.Response(404)
        return _png(request)

    media, requests, bodies, _ = _media(handler)
    (outcome,) = await media.resolve([(ISSUE, REF)])
    assert isinstance(outcome, Fetched)
    assert bodies.calls == 2
    assert [bare(str(r.url)) for r in requests] == [bare(_signed(1)), bare(_signed(2))]


async def test_an_image_still_missing_after_every_renewal_is_removed() -> None:
    media, requests, _, _ = _media(lambda _: httpx.Response(404))
    assert await media.resolve([(ISSUE, REF)]) == (Unavailable(REF, REMOVED),)
    assert len(requests) == 3


async def test_a_server_error_that_persists_defers_without_the_signature() -> None:
    media, _, _, slept = _media(lambda _: httpx.Response(503))
    with pytest.raises(MediaTransient) as excinfo:
        await media.resolve([(ISSUE, REF)])
    assert "jwt" not in str(excinfo.value)
    assert "sig" not in str(excinfo.value)
    assert slept == [0.5, 1.0]  # between the three tries, not after the last


async def test_a_transport_failure_defers_without_the_signature() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout(f"timed out reaching {request.url}")

    media, _, _, _ = _media(handler)
    with pytest.raises(MediaTransient) as excinfo:
        await media.resolve([(ISSUE, REF)])
    assert "jwt" not in str(excinfo.value)
    assert excinfo.value.__suppress_context__, "the httpx error names the signed URL"


async def test_images_past_the_count_limit_are_named_not_fetched() -> None:
    other = "0e2c1a6f-1234-4abc-9def-0123456789ab"
    (second,) = media_refs(f"https://github.com/user-attachments/assets/{other}")

    def render(n: int) -> str:
        return f'<img src="{_signed(n)}"><img src="{_signed(n).replace(UUID, other)}">'

    media, requests, _, _ = _media(_png, _Bodies(render), images=1)
    first, capped = await media.resolve([(ISSUE, REF), (ISSUE, second)])
    assert isinstance(first, Fetched)
    assert capped == Unavailable(second, "over the 1-image limit")
    assert len(requests) == 1


async def test_the_image_that_crosses_the_run_limit_is_named() -> None:
    media, _, _, _ = _media(_png, run_bytes=len(PNG) - 1)
    (outcome,) = await media.resolve([(ISSUE, REF)])
    assert isinstance(outcome, Unavailable)
    assert outcome.reason.startswith("over the ")
    assert outcome.reason.endswith(" MB run limit")


async def test_an_external_image_is_fetched_through_camo() -> None:
    (external,) = media_refs("![chart](https://i.imgur.com/abc.png?v=2)")
    camo = '<img src="https://camo.githubusercontent.com/abc/686" data-canonical-src="https://i.imgur.com/abc.png?v=2">'
    media, requests, _, _ = _media(_png, _Bodies(lambda _: camo))
    (outcome,) = await media.resolve([(ISSUE, external)])
    assert isinstance(outcome, Fetched)
    assert requests[0].url.host == "camo.githubusercontent.com"


async def test_a_resolution_past_its_budget_defers() -> None:
    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return _png(_)

    media, _, _, _ = _media(slow, budget_seconds=0.05)
    with pytest.raises(MediaTransient, match="took over"):
        await media.resolve([(ISSUE, REF)])


async def test_rendered_bodies_are_read_as_full_json_by_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_headers: list[dict[str, str]] = []

    class _Issues:
        async def async_get(self, *_: object, headers: dict[str, str], **__: object) -> object:
            seen_headers.append(headers)
            return SimpleNamespace(content=json.dumps({"body_html": "<p>issue</p>"}).encode())

        async def async_list_comments(
            self, *_: object, headers: dict[str, str], **__: object
        ) -> object:
            seen_headers.append(headers)
            rows = [{"id": 7, "body_html": "<p>seven</p>"}, {"id": 8, "body_html": None}]
            return SimpleNamespace(content=json.dumps(rows).encode())

    client = GitHubClient(None)

    async def fake_api(_repo: str) -> object:
        return SimpleNamespace(rest=SimpleNamespace(issues=_Issues()))

    monkeypatch.setattr(client, "api", fake_api)
    media = GitHubMedia(client, "acme/widgets")

    assert await media._fetch_bodies("issue", 145) == {None: "<p>issue</p>"}
    assert await media._fetch_bodies("issue_comment", 145) == {7: "<p>seven</p>", 8: ""}
    assert all(h == {"Accept": "application/vnd.github.full+json"} for h in seen_headers)


async def test_a_download_is_logged_without_its_signature(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="httpx")
    media, _, _, _ = _media(_png)
    await media.resolve([(ISSUE, REF)])

    assert "jwt" not in caplog.text
    assert bare(_signed(1)) in caplog.text
