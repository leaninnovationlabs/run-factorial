"""GitLab embedded-image parsing and fetch."""

from __future__ import annotations

import httpx
from pydantic import SecretStr

from orchestrator.github.media import ItemRef
from orchestrator.gitlab.media import GitLabMedia, gitlab_media_refs
from orchestrator.settings import GitLabSettings


def test_gitlab_media_refs_finds_uploads_and_external_images() -> None:
    refs = gitlab_media_refs(
        "see ![a](/uploads/abc/shot.png) and ![b](https://cdn.example/x.png)"
    )
    assert [ref.kind for ref in refs] == ["asset", "external"]
    assert refs[0].origin == "/uploads/abc/shot.png"


async def test_gitlab_media_fetches_an_upload_with_the_project_token() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("private-token")))
        return httpx.Response(200, content=png)

    class _Client:
        settings = GitLabSettings(
            base_url="https://gitlab.example", token=SecretStr("glpat-test")
        )

    media = GitLabMedia(
        _Client(),  # type: ignore[arg-type]
        "group/app",
        transport=httpx.MockTransport(handler),
    )
    ref = gitlab_media_refs("![a](/uploads/abc/shot.png)")[0]
    fetched = await media.resolve([(ItemRef("issue", 4), ref)])
    assert fetched[0] is not None
    assert getattr(fetched[0], "media_type", None) == "image/png"
    assert seen == [("https://gitlab.example/group/app/uploads/abc/shot.png", "glpat-test")]


async def test_gitlab_media_sends_the_token_to_the_gitlab_origin_alone() -> None:
    """An issue author picks these URLs. A prefix match sent the token to a lookalike host and
    to `gitlab.example@evil`, and httpx's redirects carried it to wherever GitLab pointed."""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("private-token")))
        if request.url.path.endswith("/moved.png"):
            return httpx.Response(302, headers={"Location": "https://evil.example/stolen.png"})
        return httpx.Response(200, content=png)

    class _Client:
        settings = GitLabSettings(base_url="https://gitlab.example", token=SecretStr("glpat-test"))

    media = GitLabMedia(
        _Client(),  # type: ignore[arg-type]
        "group/app",
        transport=httpx.MockTransport(handler),
    )
    refs = gitlab_media_refs(
        "![a](https://gitlab.example.evil.example/x.png) "
        "![b](https://gitlab.example@evil.example/y.png) "
        "![c](https://gitlab.example:8443/z.png) "
        "![d](/uploads/abc/moved.png)"
    )
    await media.resolve([(ItemRef("issue", 4), ref) for ref in refs])
    assert seen == [
        ("https://gitlab.example.evil.example/x.png", None),
        ("https://evil.example/y.png", None),
        ("https://gitlab.example:8443/z.png", None),
        ("https://gitlab.example/group/app/uploads/abc/moved.png", "glpat-test"),
        ("https://evil.example/stolen.png", None),
    ]


async def test_gitlab_media_fetches_no_address_inside_the_deployment() -> None:
    """An issue's author names these URLs, and a redirect names the next: the worker requested
    loopback, link-local and private addresses for them, which a project's GitLab URL may never
    name."""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("/moved.png"):
            return httpx.Response(
                302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
            )
        return httpx.Response(200, content=png)

    class _Client:
        settings = GitLabSettings(base_url="https://1.1.1.1", token=SecretStr("glpat-test"))

    media = GitLabMedia(
        _Client(),  # type: ignore[arg-type]
        "group/app",
        transport=httpx.MockTransport(handler),
    )
    refs = gitlab_media_refs(
        "![a](http://127.0.0.1:8080/x.png) ![b](http://[::1]/y.png) "
        "![c](http://2130706433/z.png) ![d](/uploads/abc/moved.png) "
        "![e](https://8.8.8.8/ok.png)"
    )
    outcomes = await media.resolve([(ItemRef("issue", 4), ref) for ref in refs])
    assert seen == [
        "https://1.1.1.1/group/app/uploads/abc/moved.png",
        "https://8.8.8.8/ok.png",
    ]
    assert [type(outcome).__name__ for outcome in outcomes] == [
        "Unavailable",
        "Unavailable",
        "Unavailable",
        "Unavailable",
        "Fetched",
    ]
