"""The site: the API and the dashboard under `APP_PREFIX` (tech-stack decisions 52 and 74)."""

from __future__ import annotations

import re
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from orchestrator.api.main import create_app, create_site
from orchestrator.settings import APP_PREFIX

VITE_CONFIG = Path(__file__).resolve().parents[2] / "ui" / "vite.config.ts"
INDEX = '<!doctype html><title>factorial</title><div id="root"></div>'


def _dist(root: Path) -> Path:
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX)
    (dist / "assets" / "app.js").write_text("console.log('app')")
    return dist


def _client(root: Path, *, docs: bool = False) -> AsyncClient:
    site = create_site(create_app(docs=docs, dashboard_dir=_dist(root)))
    return AsyncClient(transport=ASGITransport(app=site), base_url="http://site.test")


async def test_the_prefix_serves_the_index(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        response = await client.get(f"{APP_PREFIX}/")
    assert response.status_code == 200
    assert response.text == INDEX


async def test_an_existing_asset_is_served_as_itself(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        response = await client.get(f"{APP_PREFIX}/assets/app.js")
    assert response.text == "console.log('app')"


async def test_a_nested_client_route_is_answered_by_the_index(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        response = await client.get(f"{APP_PREFIX}/orgs/default/projects/3/tickets/12")
    assert response.status_code == 200
    assert response.text == INDEX


async def test_a_missing_asset_is_a_404_not_the_index(tmp_path: Path) -> None:
    """HTML in place of a missing bundle would hide a broken build behind a blank page."""
    async with _client(tmp_path) as client:
        response = await client.get(f"{APP_PREFIX}/assets/missing.js")
    assert response.status_code == 404


async def test_a_file_outside_the_directory_is_never_served(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("top secret")
    async with _client(tmp_path) as client:
        response = await client.get(f"{APP_PREFIX}/%2e%2e/secret.txt")
    assert "top secret" not in response.text


async def test_an_unknown_api_path_and_a_write_stay_json_404s(tmp_path: Path) -> None:
    """A page there would answer a client's typo with 200 and HTML it cannot read."""
    async with _client(tmp_path) as client:
        responses = [
            await client.get(f"{APP_PREFIX}/api/no-such-route"),
            await client.get(f"{APP_PREFIX}/auth/no-such-route"),
            await client.get(f"{APP_PREFIX}/.well-known/oauth-protected-resource"),
            await client.post(f"{APP_PREFIX}/orgs/default/overview"),
        ]
    for response in responses:
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}


async def test_a_wrong_method_is_still_a_405(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        response = await client.get(f"{APP_PREFIX}/webhooks/github/apps/1")
    assert response.status_code == 405


async def test_a_path_outside_the_prefix_moves_under_it(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        root = await client.get("/")
        old = await client.get("/orgs/acme/tickets/7?terminal=3")
    assert (root.status_code, root.headers["location"]) == (302, f"{APP_PREFIX}/")
    assert old.headers["location"] == f"{APP_PREFIX}/orgs/acme/tickets/7?terminal=3"


async def test_the_probes_answer_at_the_root_and_under_the_prefix(tmp_path: Path) -> None:
    """The target group's check goes to the root, which the image before the prefix served."""
    async with _client(tmp_path) as client:
        responses = [await client.get("/healthz"), await client.get(f"{APP_PREFIX}/healthz")]
    assert [r.json() for r in responses] == [{"status": "ok"}] * 2


async def test_the_api_docs_are_off_unless_asked(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        off = await client.get(f"{APP_PREFIX}/openapi.json")
    async with _client(tmp_path / "on", docs=True) as client:
        on = await client.get(f"{APP_PREFIX}/openapi.json")
    assert off.status_code == 404
    assert on.status_code == 200


async def test_the_index_is_never_cached_and_hashed_assets_always_are(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        root = await client.get(f"{APP_PREFIX}/")
        direct = await client.get(f"{APP_PREFIX}/index.html")
        route = await client.get(f"{APP_PREFIX}/orgs/default/overview")
        asset = await client.get(f"{APP_PREFIX}/assets/app.js")
    for page in (root, direct, route):
        assert page.headers["cache-control"] == "no-cache"
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_the_dashboard_is_built_for_the_prefix_the_api_serves_it_under() -> None:
    """Built for another, every page loads its bundle from a path that 404s: a blank page."""
    match = re.search(r'^\s*base:\s*"([^"]*)"', VITE_CONFIG.read_text(), re.MULTILINE)
    assert match is not None
    assert match.group(1) == f"{APP_PREFIX}/"
