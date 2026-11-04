"""`GET /api/version` is for a signed-in reader: the build is not for an anonymous probe."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.api.routes import version
from tests.api_harness import authed_client_for, build_app, client_for, make_settings


async def test_the_build_needs_a_session() -> None:
    app = build_app(make_settings())
    async with client_for(app) as anonymous:
        assert (await anonymous.get("/api/version")).status_code == 401
    async with authed_client_for(app) as client:
        assert (await client.get("/api/version")).status_code == 200


async def test_the_revision_is_null_when_the_image_wrote_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = build_app(make_settings())
    async with authed_client_for(app) as client:
        for text, expected in (("abc1234def\n", "abc1234def"), ("\n", None)):
            (tmp_path / "revision").write_text(text)
            monkeypatch.setattr(version, "REVISION_FILE", tmp_path / "revision")
            assert (await client.get("/api/version")).json() == {"revision": expected}
        monkeypatch.setattr(version, "REVISION_FILE", tmp_path / "missing")
        assert (await client.get("/api/version")).json() == {"revision": None}
