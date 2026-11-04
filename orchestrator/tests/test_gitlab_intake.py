"""API intake create accepts a GitLab ticket source. No Postgres."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.api.services import intake
from orchestrator.api.services.errors import Conflict
from orchestrator.db.models import TicketSourceKind


class _Writer:
    async def labels(self, repo: str) -> frozenset[str]:
        return frozenset()

    async def assignable(self, repo: str, login: str) -> bool:
        return True


async def test_preflight_accepts_a_gitlab_ticket_source(monkeypatch: pytest.MonkeyPatch) -> None:
    source = SimpleNamespace(kind=TicketSourceKind.GITLAB, external_ref="group/app")
    project = SimpleNamespace(id=1, name="app", github_repo="group/app", active=True)

    async def fake_source(_session: object, _project_id: int) -> object:
        return source

    async def past_check(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("past-source-check")

    monkeypatch.setattr(intake, "ticket_source", fake_source)
    monkeypatch.setattr(intake, "lane_for", past_check)

    with pytest.raises(RuntimeError, match="past-source-check"):
        await intake._preflight(
            None,  # type: ignore[arg-type]
            _Writer(),  # type: ignore[arg-type]
            project,  # type: ignore[arg-type]
            title="Fix it",
            body="please",
            assignee="operator",
            process=None,
        )


async def test_preflight_still_refuses_a_project_with_no_ticket_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = SimpleNamespace(id=1, name="app", github_repo="acme/app", active=True)

    async def fake_source(_session: object, _project_id: int) -> None:
        return None

    monkeypatch.setattr(intake, "ticket_source", fake_source)

    with pytest.raises(Conflict, match="no GitHub ticket source"):
        await intake._preflight(
            None,  # type: ignore[arg-type]
            _Writer(),  # type: ignore[arg-type]
            project,  # type: ignore[arg-type]
            title="Fix it",
            body="please",
            assignee="operator",
            process=None,
        )
