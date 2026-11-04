"""Parked tickets observed as a set (tech-stack.md decision 30).

A parked ticket — blocked, interrupted, waiting, exited — asks GitHub two things a tick: is my
parking label still on the issue, and is the issue open. One listing per (repo, parking label)
answers both for every parked ticket in the repo, so the cost is flat in tickets. The one other
read is a parked session's postconditions, at most every `terminal.parked_recheck`
(`agent_steps.recheck_parked`). `ParkedSource` is
that listing, read through the project's own client; `ParkedListings` is one tick's cache over
it, keyed by client as well as repo because two organizations may point at one repo with
different credentials. It is threaded into each reconcile beside the lease values because
`ReconcileDeps` outlives many ticks and a listing is true for one.

A sibling of `admission.CandidateSource`, and kept out of `StateSource` for the reason that
module's docstring gives: every predicate call goes through that interface, and a repo-wide
fetch does not belong behind it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from orchestrator.db.models import TicketStatus
from orchestrator.github import LABEL_PREFIX, LABEL_PREFIXES
from orchestrator.github.client import GitHubClient
from orchestrator.github.observer import list_issues
from orchestrator.github.state import AsyncKeyedMemo, IssueState

__all__ = [
    "PARKING_LABELS_BY_STATUS",
    "GitHubParked",
    "InMemoryParked",
    "ParkedFactory",
    "ParkedListings",
    "ParkedSource",
]

logger = logging.getLogger(__name__)


def _listings(*suffixes: str) -> tuple[str, ...]:
    """The labels to look a parked ticket up under, in lookup order.

    Every vocabulary, because a ticket the loop cannot find in any listing is read as released
    and acted on — missing a spelling costs a wrongly-resumed ticket, not a call. The prefix the
    projector WRITES goes first, so a ticket whose issue the loop has already re-labelled costs
    the one call it always did, and the legacy listing is only reached for an issue still
    carrying the old name (issue #138).
    """
    ordered = (LABEL_PREFIX, *(p for p in LABEL_PREFIXES if p != LABEL_PREFIX))
    return tuple(f"{prefix}{suffix}" for prefix in ordered for suffix in suffixes)


#: Which listings a parked ticket can be found in, in lookup order. Blocked is three suffixes
#: because an interrupt and an idle park park as BLOCKED too (decisions 26, 56); the first
#: listing that has the issue wins, and a repo with no such tickets never pays for the others.
PARKING_LABELS_BY_STATUS: Final[Mapping[str, tuple[str, ...]]] = {
    TicketStatus.EXITED: _listings("exit"),
    TicketStatus.BLOCKED: _listings("blocked", "interrupted", "waiting"),
}

#: How many pages one listing will pay for. The cap bounds the paging cost, not the answer: the
#: pages that arrived still serve the tickets in them, and a ticket past the cap misses and reads
#: for itself.
_MAX_PAGES: Final = 10


class ParkedSource(Protocol):
    async def labelled_issues(self, repo: str, label: str) -> Sequence[IssueState] | None:
        """Every issue in `repo` carrying `label`, open and closed, or as many of them as the
        source will page for — or None when the listing cannot be trusted at all. None is the
        fail-safe: the caller falls back to the per-ticket read and pays today's cost, rather
        than misreading a listing that is short as a label that came off."""
        ...


#: A project's client to the listing read through it. `GitHubParked` is the production one.
ParkedFactory = Callable[[GitHubClient], ParkedSource]


@dataclass(frozen=True, slots=True)
class InMemoryParked:
    """A `ParkedSource` built from literals — what tests drive the cache with."""

    issues_by_repo: Mapping[str, Sequence[IssueState]] = field(default_factory=dict)
    #: Repos whose listing fails this tick.
    unavailable: frozenset[str] = frozenset()

    async def labelled_issues(self, repo: str, label: str) -> tuple[IssueState, ...] | None:
        if repo in self.unavailable:
            return None
        return tuple(issue for issue in self.issues_by_repo.get(repo, ()) if label in issue.labels)


@dataclass(frozen=True, slots=True)
class GitHubParked:
    """The production `ParkedSource`: every issue under one label, open or closed."""

    client: GitHubClient

    async def labelled_issues(self, repo: str, label: str) -> tuple[IssueState, ...] | None:
        try:
            return await list_issues(
                self.client,
                repo,
                state="all",
                labels=label,
                listing=f"parked:{label}",
                max_pages=_MAX_PAGES,
            )
        except Exception:
            # Broad on purpose: whatever GitHub or the client raised, the answer is the same —
            # this tick pays per ticket, and the ticket that reads for itself is the authority.
            logger.exception(
                "parked listing for %s under %s failed; its tickets fall back to per-ticket "
                "reads this tick",
                repo,
                label,
            )
            return None


class ParkedListings:
    """One tick's cache of parked listings, memoised per (client, repo, label).

    Built by `loop.tick` and threaded into `reconcile_ticket`; never held on `ReconcileDeps`.
    Counts what it did so the tick can log one line the canary reads.
    """

    __slots__ = ("_memo", "_source", "failed", "fell_back", "listings", "served")

    def __init__(self, source: ParkedFactory) -> None:
        self._source = source
        self._memo: AsyncKeyedMemo[
            tuple[GitHubClient, str, str], Mapping[int, IssueState] | None
        ] = AsyncKeyedMemo(self._load)
        self.listings = 0
        self.served = 0
        self.fell_back = 0
        #: Listings the source could not answer. Counted apart from `fell_back`, which a ticket
        #: in transition raises too: only this one tells an outage from a busy tick.
        self.failed = 0

    async def _load(
        self, key: tuple[GitHubClient, str, str]
    ) -> Mapping[int, IssueState] | None:
        client, repo, label = key
        self.listings += 1
        issues = await self._source(client).labelled_issues(repo, label)
        if issues is None:
            self.failed += 1
            return None
        return {issue.number: issue for issue in issues}

    async def issue(
        self, client: GitHubClient, repo: str, number: int, labels: Sequence[str]
    ) -> IssueState | None:
        """The issue as its parking listing has it, or None — the caller then reads it itself.

        A hit means the label is on the issue by construction and its state is beside it. A
        miss means the ticket is in transition, or sits past the listing's page cap, or the
        listing was unavailable; all three are the per-ticket read's to settle.
        """
        for label in labels:
            listing = await self._memo.get((client, repo, label))
            if listing is not None and number in listing:
                self.served += 1
                return listing[number]
        self.fell_back += 1
        return None
