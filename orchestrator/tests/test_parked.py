"""The parked listing seam (decision 30): one listing per (repo, label), memoised per tick,
with a miss or a failed listing handing the ticket back to its own read.

No Postgres: everything here is the seam and the cache, exercised with literals and a fake
issues API the same shape `test_admission_candidates.py` uses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from orchestrator.db.models import TicketStatus
from orchestrator.github import (
    EXIT_LABELS,
    LABEL_BLOCKED,
    LABEL_EXIT,
    LABEL_INTERRUPTED,
    LABEL_PREFIX,
    PARKING_LABELS,
)
from orchestrator.github.client import GitHubClient
from orchestrator.github.state import IssueState
from orchestrator.worker.parked import (
    PARKING_LABELS_BY_STATUS,
    GitHubParked,
    InMemoryParked,
    ParkedListings,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

REPO = "acme/widgets"
CLIENT = GitHubClient(None)


def _state(number: int, *labels: str, state: str = "open") -> IssueState:
    return IssueState(
        number=number, title="t", body="", state=state, labels=frozenset(labels), assignees=()
    )


def _raw(number: int, *labels: str, state: str = "open", **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "number": number,
        "title": "t",
        "body": "",
        "state": state,
        "labels": [{"name": label} for label in labels],
        "assignees": [],
    }
    payload.update(fields)
    return payload


@dataclass
class _Response:
    content: bytes


class _FakeIssuesApi:
    """Records every listing call, including the label filter, and serves pages."""

    def __init__(self, pages: list[list[dict[str, Any]]], *, error: Exception | None = None):
        self._pages = pages
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def async_list_for_repo(
        self,
        owner: str,
        repo: str,
        *,
        state: str,
        labels: str,
        creator: object,
        since: object,
        direction: object,
        per_page: int,
        page: int,
    ) -> _Response:
        self.calls.append({"state": state, "labels": labels, "per_page": per_page, "page": page})
        if self._error is not None:
            raise self._error
        index = page - 1
        body = self._pages[index] if index < len(self._pages) else []
        return _Response(content=json.dumps(body).encode())


class _FakeClient:
    def __init__(self, api: _FakeIssuesApi) -> None:
        self.issues = api

    async def api(self, _repo: str) -> Any:
        rest = type("_Rest", (), {"issues": self.issues})()
        return type("_Api", (), {"rest": rest})()


def _parked(pages: list[list[dict[str, Any]]], **kw: Any) -> tuple[GitHubParked, _FakeIssuesApi]:
    api = _FakeIssuesApi(pages, **kw)
    return GitHubParked(client=_FakeClient(api)), api  # type: ignore[arg-type]


# --- the production listing -------------------------------------------------------------------


async def test_the_listing_asks_for_every_state_under_one_label() -> None:
    """`state=all` because an exited ticket's issue is usually closed, and the label filter is
    what makes one call cover every parked ticket in the repo."""
    parked, api = _parked([[_raw(1, LABEL_EXIT, state="closed"), _raw(2, LABEL_EXIT)]])

    issues = await parked.labelled_issues(REPO, LABEL_EXIT)

    assert issues is not None
    assert [(i.number, i.state) for i in issues] == [(1, "closed"), (2, "open")]
    assert api.calls[0]["state"] == "all"
    assert api.calls[0]["labels"] == LABEL_EXIT


async def test_pull_requests_are_dropped_like_admission_drops_them() -> None:
    parked, _ = _parked([[_raw(1, LABEL_EXIT), _raw(2, LABEL_EXIT, pull_request={"url": "x"})]])
    issues = await parked.labelled_issues(REPO, LABEL_EXIT)
    assert issues is not None
    assert [i.number for i in issues] == [1]


async def test_a_failed_listing_is_none_not_an_exception() -> None:
    """None is the fail-safe: the caller falls back to per-ticket reads and pays today's cost.
    Raising would take the whole tick down with it."""
    parked, _ = _parked([], error=RuntimeError("github is having a day"))
    assert await parked.labelled_issues(REPO, LABEL_EXIT) is None


async def test_a_malformed_repo_is_none_not_an_exception() -> None:
    """The same fail-safe as a failed listing: a bad repo string must miss, not escape the
    cache's memo and be re-tried by the next parked ticket."""
    parked, _ = _parked([])
    assert await parked.labelled_issues("not-a-repo", LABEL_EXIT) is None


async def test_past_the_page_cap_the_pages_that_arrived_still_serve() -> None:
    """The cap bounds the paging cost, not the answer: a hit is positive evidence whatever page
    it came from, and a ticket beyond the cap misses and reads for itself."""
    full = [[_raw(n + 100 * p, LABEL_EXIT) for n in range(100)] for p in range(11)]
    parked, api = _parked(full)

    issues = await parked.labelled_issues(REPO, LABEL_EXIT)

    assert issues is not None and len(issues) == 1000
    assert len(api.calls) == 10


# --- the per-tick cache -----------------------------------------------------------------------


class _Counting:
    """A `ParkedSource` over `InMemoryParked` that counts its listing calls, so a test can
    prove memoisation. Composed rather than subclassed: `InMemoryParked` is frozen and
    slotted, and the protocol asks for one method."""

    def __init__(self, **kwargs: Any) -> None:
        self.inner = InMemoryParked(**kwargs)
        self.calls: list[tuple[str, str]] = []

    async def labelled_issues(self, repo: str, label: str) -> tuple[IssueState, ...] | None:
        self.calls.append((repo, label))
        return await self.inner.labelled_issues(repo, label)


async def test_two_tickets_under_one_label_cost_one_listing() -> None:
    source = _Counting(issues_by_repo={REPO: (_state(1, LABEL_EXIT), _state(2, LABEL_EXIT))})
    cache = ParkedListings(lambda _client: source)

    first = await cache.issue(CLIENT, REPO, 1, (LABEL_EXIT,))
    second = await cache.issue(CLIENT, REPO, 2, (LABEL_EXIT,))

    assert (first.number, second.number) == (1, 2)  # type: ignore[union-attr]
    assert source.calls == [(REPO, LABEL_EXIT)]
    assert (cache.listings, cache.served, cache.fell_back, cache.failed) == (1, 2, 0, 0)


async def test_a_ticket_absent_from_its_listing_is_a_miss() -> None:
    """The label came off, or the issue moved: the caller reads it itself."""
    listing = InMemoryParked(issues_by_repo={REPO: (_state(1, LABEL_EXIT),)})
    cache = ParkedListings(lambda _client: listing)
    assert await cache.issue(CLIENT, REPO, 2, (LABEL_EXIT,)) is None
    assert cache.fell_back == 1


async def test_an_unavailable_repo_misses_every_ticket() -> None:
    cache = ParkedListings(lambda _client: InMemoryParked(unavailable=frozenset({REPO})))
    assert await cache.issue(CLIENT, REPO, 1, (LABEL_EXIT,)) is None
    assert (cache.listings, cache.served, cache.fell_back, cache.failed) == (1, 0, 1, 1)


async def test_each_client_reads_its_own_listing_of_one_repo() -> None:
    """Two organizations may point at one repo: each project's listing is read through its own
    credentials, never served from another's."""
    sources = {CLIENT: _Counting(issues_by_repo={REPO: (_state(1, LABEL_EXIT),)})}
    other = GitHubClient(None)
    sources[other] = _Counting(issues_by_repo={REPO: (_state(1, LABEL_EXIT),)})
    cache = ParkedListings(sources.__getitem__)

    await cache.issue(CLIENT, REPO, 1, (LABEL_EXIT,))
    await cache.issue(other, REPO, 1, (LABEL_EXIT,))

    assert sources[CLIENT].calls == sources[other].calls == [(REPO, LABEL_EXIT)]


async def test_a_blocked_ticket_is_looked_for_under_blocked_then_interrupted() -> None:
    source = _Counting(issues_by_repo={REPO: (_state(7, LABEL_INTERRUPTED),)})
    cache = ParkedListings(lambda _client: source)

    found = await cache.issue(CLIENT, REPO, 7, PARKING_LABELS_BY_STATUS[TicketStatus.BLOCKED])

    assert found is not None and LABEL_INTERRUPTED in found.labels
    assert source.calls == [(REPO, LABEL_BLOCKED), (REPO, LABEL_INTERRUPTED)]


def test_the_status_to_labels_table_is_closed() -> None:
    """Only the two parked statuses have parking labels; a running or done ticket never
    consults a listing."""
    assert set(PARKING_LABELS_BY_STATUS) == {TicketStatus.BLOCKED, TicketStatus.EXITED}
    assert set(PARKING_LABELS_BY_STATUS[TicketStatus.EXITED]) == EXIT_LABELS
    # This table restates `PARKING_LABELS` with an order; drift between them would be silent.
    assert set(PARKING_LABELS_BY_STATUS[TicketStatus.BLOCKED]) == PARKING_LABELS
    # The vocabulary the projector writes is looked up FIRST, so an issue the loop has already
    # re-labelled costs the one call it always did and never reaches the legacy listing.
    for labels in PARKING_LABELS_BY_STATUS.values():
        assert labels[0].startswith(LABEL_PREFIX)
