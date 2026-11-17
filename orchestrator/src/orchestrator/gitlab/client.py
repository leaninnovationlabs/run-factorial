"""HTTP client for a self-hosted GitLab. Token auth only — no GitHub App, no Okta."""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

import httpx

from orchestrator.settings import GitLabSettings

__all__ = ["GitLabClient", "GitLabError", "quote_project"]


class GitLabError(RuntimeError):
    def __init__(
        self, message: str, *, status_code: int | None = None, body: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        #: The response text, whole: `message` keeps only its first 300 characters.
        self.body = body


def quote_project(repo: str) -> str:
    return quote(repo.strip().strip("/"), safe="")


def tls_verify(settings: GitLabSettings) -> ssl.SSLContext | bool:
    """System CAs plus any extra bundle. Path or PEM; any private PKI, not a vendor-specific
    list."""
    pem = (settings.ca_pem or "").strip()
    path = settings.ca_bundle
    if path is not None and path.exists() and path.read_text().strip().startswith("-----BEGIN"):
        pem = f"{pem}\n{path.read_text()}".strip() if pem else path.read_text()
        path = None
    if not pem and path is None:
        return True
    ctx = ssl.create_default_context()
    if pem:
        ctx.load_verify_locations(cadata=pem)
    if path is not None:
        ctx.load_verify_locations(cafile=str(path.expanduser()))
    return ctx


class GitLabClient:
    """One project's client (`project_client.client_for_project`). The PAT is read from its
    settings; it is never a human Okta session."""

    def __init__(self, settings: GitLabSettings) -> None:
        self._settings = settings
        self._http: httpx.AsyncClient | None = None
        self._in_flight = 0
        self._closing = False

    @property
    def settings(self) -> GitLabSettings:
        return self._settings

    def _require(self) -> GitLabSettings:
        if not self._settings.configured:
            raise RuntimeError(
                "GitLab is not configured: set FACTORIAL_GITLAB__BASE_URL and "
                "FACTORIAL_GITLAB__TOKEN"
            )
        return self._settings

    async def http(self) -> httpx.AsyncClient:
        if self._http is None:
            s = self._require()
            self._http = httpx.AsyncClient(
                base_url=s.api_url,
                headers={"PRIVATE-TOKEN": s.token.get_secret_value()},
                timeout=30.0,
                verify=tls_verify(s),
            )
        return self._http

    async def aclose(self) -> None:
        """Close the HTTP client now, or when the last request in flight ends. A caller still
        holding this client afterwards gets a connection for each request, closed as it ends."""
        self._closing = True
        if not self._in_flight:
            await self._close_http()

    async def _close_http(self) -> None:
        http, self._http = self._http, None
        if http is not None:
            await http.aclose()

    @asynccontextmanager
    async def _held(self) -> AsyncIterator[httpx.AsyncClient]:
        """The HTTP client for one request, which `aclose` waits for."""
        http = await self.http()
        self._in_flight += 1
        try:
            yield http
        except httpx.ConnectError as exc:
            raise GitLabError(
                f"cannot reach GitLab at {self._settings.base_url}: {exc}. "
                "If this host uses a private CA, paste its PEM on the project (a file path "
                "only on a single-organization deployment) or set FACTORIAL_GITLAB__CA_BUNDLE.",
                status_code=None,
            ) from exc
        finally:
            self._in_flight -= 1
            if self._closing and not self._in_flight:
                await self._close_http()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        async with self._held() as http:
            response = await http.request(method, path, params=params, json=json)
        _raise_for(method, path, response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    @asynccontextmanager
    async def stream(self, url: str, **params: Any) -> AsyncIterator[httpx.Response]:
        """A GET whose body the caller reads as it arrives, so it can stop early. Fails as
        `request` does."""
        async with self._held() as http, http.stream("GET", url, params=params or None) as response:
            if response.status_code >= 400:
                await response.aread()
            _raise_for("GET", url, response)
            yield response

    async def get(self, url: str, **params: Any) -> Any:
        # `url` not `path`: GitLab's tree API takes a query parameter named `path`, and a
        # parameter of that name would swallow it (`paginate() got multiple values for path`).
        return await self.request("GET", url, params=params or None)

    async def paginate(self, url: str, **params: Any) -> list[Any]:
        page = 1
        out: list[Any] = []
        while True:
            batch = await self.get(url, page=page, per_page=100, **params)
            if not isinstance(batch, list) or not batch:
                return out
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1
            if page > 20:
                return out


def _raise_for(method: str, path: str, response: httpx.Response) -> None:
    if response.status_code == 404:
        raise GitLabError(f"{method} {path} not found", status_code=404)
    if response.status_code >= 400:
        raise GitLabError(
            f"{method} {path} failed: {response.status_code} {response.text[:300]}",
            status_code=response.status_code,
            body=response.text,
        )
