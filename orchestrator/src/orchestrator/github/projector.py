"""`project` — writing orchestrator state back to GitHub (design.md §3.1).

These are the only GitHub mutations the orchestrator makes. Agents make their own — their own
branch, their own PR — with their own per-run token (tech-stack decision 10); nothing here acts on
an agent's behalf.

GitHub is a *projection* of state the orchestrator owns (design.md §2 decision 2), so every
argument below is an already-resolved concrete value: template expansion (`{ticket}`,
`{roles.<name>}`, `{slug}`) belongs to the config layer, and a template reaching this module means
a layer boundary was crossed.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final, Literal

from githubkit.exception import RequestFailed
from githubkit.utils import UNSET
from githubkit_schemas.latest.types import (
    ReposOwnerRepoIssuesIssueNumberPatchBodyPropAssigneesItemsOneof1Type as _AssigneeRef,
)

from orchestrator.adapters.merges import MergeRefused, refusal_message
from orchestrator.github.client import GitHubClient, split_repo

__all__ = [
    "BLOCKED_LABELS",
    "DONE_LABELS",
    "EXIT_LABELS",
    "INTERRUPTED_LABELS",
    "LABEL_BLOCKED",
    "LABEL_DONE",
    "LABEL_EXIT",
    "LABEL_INTERRUPTED",
    "LABEL_PREFIX",
    "LABEL_PREFIXES",
    "LABEL_WAITING",
    "PARKING_LABELS",
    "WAITING_LABELS",
    "GitHubProjector",
    "MergeRefused",
    "desired_status_labels",
    "step_from_label",
    "step_label",
]

#: Heads a pull request comment that carries a reviewer's dashboard change request (design.md
#: §7.8). It is posted under the App's login, which `pr_comment_contains` otherwise trusts as
#: delivery evidence, so that predicate skips a comment that starts with this.
CHANGE_REQUEST_MARKER: Final = "<!-- factorial:change-request -->"

#: What the projector WRITES. One prefix, because an issue must carry one vocabulary, not two.
LABEL_PREFIX: Final = "factorial:"
#: What the projector RECOGNISES as its own. Both names, permanently while any repo or open
#: issue still carries the old one (issue #138). Two different things depend on this:
#: `desired_status_labels`'s sweep decides which labels are the orchestrator's to REMOVE — a
#: prefix missing here reads as a human's label and is kept for ever — and the `*_LABELS` sets
#: below decide which labels the loop READS as a gesture.
LABEL_PREFIXES: Final = ("adlc:", "factorial:")


def _vocabularies(suffix: str) -> frozenset[str]:
    """Every spelling of one status label. The loop recognises all of them and writes one.

    A parked ticket is recognised by its label being PRESENT, and REMOVAL is the release
    gesture. So a set that knows only the current vocabulary reads an issue still carrying the
    old spelling as already released, and acts on it — which on a rename is every parked ticket
    in the fleet, on the same tick.
    """
    return frozenset(f"{prefix}{suffix}" for prefix in LABEL_PREFIXES)


LABEL_BLOCKED: Final = f"{LABEL_PREFIX}blocked"
BLOCKED_LABELS: Final = _vocabularies("blocked")

#: A run a human stopped from the dashboard (decision 26). Parks the ticket exactly as
#: `LABEL_BLOCKED` does — removing it is the release — but says a different thing happened,
#: and releases into a resume rather than a fresh attempt.
LABEL_INTERRUPTED: Final = f"{LABEL_PREFIX}interrupted"
INTERRUPTED_LABELS: Final = _vocabularies("interrupted")

#: A run idle-parked waiting on a human (decisions 26, 56). Parks and releases like
#: `LABEL_INTERRUPTED`, and like it sits beside the step label.
LABEL_WAITING: Final = f"{LABEL_PREFIX}waiting"
WAITING_LABELS: Final = _vocabularies("waiting")

#: A ticket taken off the factory floor (decision 29). Parks like the three above — removing it
#: is the release — but with its own status, `exited`, so a reader can tell "sent away" from
#: "escalated" without reading the issue. NOT in `PARKING_LABELS`: that set answers "is a
#: BLOCKED ticket still waiting", and an exited ticket is not blocked.
LABEL_EXIT: Final = f"{LABEL_PREFIX}exit"
EXIT_LABELS: Final = _vocabularies("exit")

#: Labels whose presence means a BLOCKED ticket is still waiting on a human. A parked ticket
#: is recognised by a label being PRESENT, so a parking label missing from this set makes the
#: loop read the ticket as already released and act on it immediately.
PARKING_LABELS: Final = BLOCKED_LABELS | INTERRUPTED_LABELS | WAITING_LABELS
LABEL_DONE: Final = f"{LABEL_PREFIX}done"
DONE_LABELS: Final = _vocabularies("done")

#: Reserved suffixes. A step named `blocked` or `done` would collide with the status labels, so
#: config validation should reject those names rather than this module disambiguating at runtime.
_RESERVED: Final = BLOCKED_LABELS | DONE_LABELS | EXIT_LABELS


def step_label(step: str) -> str:
    label = f"{LABEL_PREFIX}{step}"
    if label in _RESERVED:
        raise ValueError(f"step name {step!r} collides with the reserved status label {label!r}")
    return label


def step_from_label(label: str) -> str | None:
    """The step name inside a config's `ticketLabel`, or None if it names no step at all.

    The inverse of `step_label`, and it accepts either vocabulary: a pinned config still says
    `adlc:<step>` while a freshly synced one says `factorial:<step>`, and both mean the same
    step. Which prefix the issue then CARRIES is `step_label`'s decision, not this one's.
    """
    for prefix in LABEL_PREFIXES:
        if label.startswith(prefix):
            return label.removeprefix(prefix)
    return None


def desired_status_labels(
    current: Iterable[str],
    *,
    step: str | None = None,
    blocked: bool = False,
    interrupted: bool = False,
    waiting: bool = False,
    exited: bool = False,
    done: bool = False,
) -> frozenset[str]:
    """The full label set to `PUT` on the issue.

    Rules: exactly one `factorial:<step>` label when a step is given, plus `factorial:blocked` /
    `factorial:interrupted` / `factorial:waiting` / `factorial:exit` / `factorial:done` when those
    hold; every label this module recognises as its own is dropped, and every non-`factorial:`
    label is preserved untouched — humans own those, and observing a human's label is not a
    licence to delete it (design.md §2 decision 2).

    Returned as a set, not a diff, because GitHub's set-labels call is a replacement: computing
    the whole desired set here is what makes the projection idempotent.
    """
    kept = {label for label in current if not label.startswith(LABEL_PREFIXES)}
    if step is not None:
        kept.add(step_label(step))
    if blocked:
        kept.add(LABEL_BLOCKED)
    if interrupted:
        # A STATUS label beside the step label, exactly like `blocked` — not a step label itself.
        # Without this flag the caller's only route is the generic `factorial:`-prefix branch, which
        # reads "interrupted" as a step NAME and writes `factorial:interrupted` INSTEAD of
        # `factorial:build`, losing the one thing a reader wants from a parked ticket: where it
        # stopped.
        kept.add(LABEL_INTERRUPTED)
    if waiting:
        kept.add(LABEL_WAITING)
    if exited:
        kept.add(LABEL_EXIT)
    if done:
        kept.add(LABEL_DONE)
    return frozenset(kept)


class GitHubProjector:
    """Mutations against one repository, as the App installation."""

    def __init__(self, client: GitHubClient, repo: str) -> None:
        self.repo = repo
        self._client = client
        self._owner, self._name = split_repo(repo)

    async def set_labels(self, issue_number: int, labels: Iterable[str]) -> None:
        """Replace the issue's labels with exactly `labels` — pair with `desired_status_labels`,
        which is what keeps human-owned labels in the set."""
        api = await self._client.api(self.repo)
        await api.rest.issues.async_set_labels(
            self._owner, self._name, issue_number, labels=sorted(labels)
        )

    async def set_assignees(self, issue_number: int, logins: Iterable[str]) -> None:
        """REPLACES the assignee list; it does not add to it.

        An outcome's `assign` replaces assignees so the ticket always sits with whoever must act
        next (design.md §4). `PATCH` rather than the add/remove pair, because replacement in one
        call cannot half-apply.
        """
        api = await self._client.api(self.repo)
        # The generated parameter is an invariant `list[str | <assignee ref>]`; widen the element
        # type here rather than letting the union leak into this module's signature.
        assignees: list[str | _AssigneeRef] = [login for login in sorted(logins)]
        await api.rest.issues.async_update(
            self._owner, self._name, issue_number, assignees=assignees
        )

    async def comment(self, issue_number: int, body: str) -> None:
        """Announce a transition on the ticket, or on a pull request by its number. Callers post
        only on an actual state change — the loop runs every tick and would otherwise be a
        comment firehose."""
        api = await self._client.api(self.repo)
        await api.rest.issues.async_create_comment(self._owner, self._name, issue_number, body=body)

    async def close_issue(self, issue_number: int) -> None:
        """Close the ticket at `terminal.actions.closeTicket` (design.md §4).

        Separate from `set_labels` because closing is the one projection that is not a
        convergence: a human reopening a closed issue is observed input, and the loop must not
        fight it — only the terminal step closes.
        """
        api = await self._client.api(self.repo)
        await api.rest.issues.async_update(self._owner, self._name, issue_number, state="closed")

    async def merge_pr(
        self,
        pr_number: int,
        *,
        merge_method: Literal["merge", "squash", "rebase"] = "squash",
        commit_title: str | None = None,
        sha: str | None = None,
    ) -> str:
        """Merge an approved PR at a gate's `onApprove` (design.md §4). Returns the merge sha.

        `sha` is the commit the approval was given on, and GitHub merges only while the head is
        still there, so nothing pushed after the approval lands. `main` being protected and the
        App being unable to approve its own PR is what structurally prevents a bad merge
        (design.md §7.3) — this call only exercises it.

        Raises `MergeRefused` when GitHub declines: 405 (branch protection, conflicts, a merge
        method the repo forbids) and 409 (the head moved off `sha`).
        """
        api = await self._client.api(self.repo)
        try:
            result = await api.rest.pulls.async_merge(
                self._owner,
                self._name,
                pr_number,
                merge_method=merge_method,
                commit_title=commit_title if commit_title is not None else UNSET,
                sha=sha if sha is not None else UNSET,
            )
        except RequestFailed as exc:
            status = exc.response.status_code
            if status not in _MERGE_REFUSALS:
                raise
            raise MergeRefused(status, _refusal_message(exc)) from exc
        return result.parsed_data.sha


#: The merge endpoint's answers for "not now": the PR cannot be merged as it stands (405), or
#: its head is no longer the requested `sha` (409). Every other failure is infrastructure.
_MERGE_REFUSALS: Final = frozenset({405, 409})

def _refusal_message(exc: RequestFailed) -> str:
    try:
        body = exc.response.json()
    except Exception:  # a refusal with no JSON body still has to say something
        body = None
    return refusal_message(exc.response.status_code, body)
