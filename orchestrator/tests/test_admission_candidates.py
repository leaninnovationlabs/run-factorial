"""`GitHubCandidates` — the boundary between GitHub's raw issue payloads and `IssueState`.

No network and no database: the point is the mapping and the two rules that fail silently if
they regress — pull requests must not be admitted as tickets, and paging must count the raw
batch rather than what survived the filter.

The fakes speak raw JSON (`.content` bytes), matching the production path: `open_issues`
never touches `.parsed_data`, so the typed layer's UNSET-vs-None sentinel class — which killed
admission live on the first shadow run (issue #93, 2026-08-21) — cannot come back. On plain
JSON, a plain issue simply has no `pull_request` key.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import pytest

from orchestrator.worker.admission import GitHubCandidates


def _issue(number: int, **fields: Any) -> dict[str, Any]:
    """One raw open-issue payload, the mapped fields defaulted sanely."""
    payload: dict[str, Any] = {
        "number": number,
        "title": "a ticket",
        "body": "body",
        "state": "open",
        "labels": [],
        "assignees": [],
    }
    payload.update(fields)
    return payload


@dataclass
class _Response:
    content: bytes


class _FakeIssuesApi:
    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self._pages = pages
        self.calls: list[dict[str, Any]] = []

    async def async_list_for_repo(
        self,
        owner: str,
        repo: str,
        *,
        state: str,
        labels: object,
        creator: object,
        since: object,
        direction: object,
        per_page: int,
        page: int,
    ) -> _Response:
        self.calls.append(
            {"owner": owner, "repo": repo, "state": state, "per_page": per_page, "page": page}
        )
        index = page - 1
        body = self._pages[index] if index < len(self._pages) else []
        return _Response(content=json.dumps(body).encode())


class _FakeClient:
    """Stands in for `GitHubClient`; only `api()` is reached."""

    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self.issues = _FakeIssuesApi(pages)

    async def api(self, _repo: str) -> Any:
        rest = type("_Rest", (), {"issues": self.issues})()
        return type("_Api", (), {"rest": rest})()


def _candidates(pages: list[list[dict[str, Any]]]) -> tuple[GitHubCandidates, _FakeClient]:
    client = _FakeClient(pages)
    return GitHubCandidates(client=client), client  # type: ignore[arg-type]


async def test_pull_requests_are_never_admitted_as_tickets() -> None:
    """`GET /issues` returns PRs too — admitting one would put the agent's own PR in the process."""
    candidates, _ = _candidates(
        [[_issue(1), _issue(2, pull_request={"url": "https://api.github.test/pulls/2"}), _issue(3)]]
    )
    issues = await candidates.open_issues("acme/widgets")
    assert [issue.number for issue in issues] == [1, 3]


async def test_a_plain_issue_has_no_pull_request_key_and_survives() -> None:
    """Live-API shape: a plain issue's payload simply lacks `pull_request`. The typed layer's
    UNSET sentinel made "absent" a third state and the old `is not None` filter read it as
    "this is a PR", silently dropping every open issue — admission structurally dead against the
    real API while every fixture-driven test stayed green (issue #93, 2026-08-21). Raw JSON has
    exactly two spellings — key absent, or null — and both must admit."""
    candidates, _ = _candidates([[_issue(93)]])
    issues = await candidates.open_issues("acme/widgets")
    assert [issue.number for issue in issues] == [93]


async def test_a_falsy_third_spelling_of_absence_still_admits() -> None:
    """The filter is truthiness, not `is not None`, because the incident this line memorializes
    was a third spelling of absence appearing. `false` never occurs on the wire today — but if it
    ever does, it must read as a plain issue, while a truthy stub keeps reading as a PR."""
    candidates, _ = _candidates(
        [[_issue(95, pull_request=False), _issue(96, pull_request={"url": "x"})]]
    )
    issues = await candidates.open_issues("acme/widgets")
    assert [issue.number for issue in issues] == [95]


async def test_a_null_pull_request_also_reads_as_a_plain_issue() -> None:
    """Defensive: an explicit `"pull_request": null` never appears on the wire today, but both
    spellings of absence must stay admissible."""
    candidates, _ = _candidates([[_issue(94, pull_request=None)]])
    issues = await candidates.open_issues("acme/widgets")
    assert [issue.number for issue in issues] == [94]


async def test_labels_normalise_to_the_same_shape_the_observer_produces() -> None:
    candidates, _ = _candidates(
        [[_issue(1, labels=["adlc:new", {"name": "bug"}, {"name": None}])]]
    )
    (issue,) = await candidates.open_issues("acme/widgets")
    # The nameless label is dropped, not carried through as an empty string.
    assert issue.labels == frozenset({"adlc:new", "bug"})


async def test_body_and_assignees_default_cleanly() -> None:
    candidates, _ = _candidates([[_issue(7, body=None, assignees=None)]])
    (issue,) = await candidates.open_issues("acme/widgets")
    assert issue.body == ""
    assert issue.assignees == ()


async def test_assignees_are_logins_in_order() -> None:
    candidates, _ = _candidates(
        [[_issue(7, assignees=[{"login": "operator"}, {"login": "hubot"}])]]
    )
    (issue,) = await candidates.open_issues("acme/widgets")
    assert issue.assignees == ("operator", "hubot")


async def test_paging_counts_the_raw_batch_not_the_survivors() -> None:
    """A full page of PRs yields nothing but must still advance to the next page."""
    full_page_of_prs = [_issue(n, pull_request={"url": "x"}) for n in range(100)]
    candidates, client = _candidates([full_page_of_prs, [_issue(999)]])

    issues = await candidates.open_issues("acme/widgets")

    assert [issue.number for issue in issues] == [999]
    assert [call["page"] for call in client.issues.calls] == [1, 2]


async def test_a_short_page_ends_the_walk() -> None:
    candidates, client = _candidates([[_issue(1)]])
    await candidates.open_issues("acme/widgets")
    assert len(client.issues.calls) == 1
    assert client.issues.calls[0]["state"] == "open"
    assert client.issues.calls[0] == {
        "owner": "acme",
        "repo": "widgets",
        "state": "open",
        "per_page": 100,
        "page": 1,
    }


async def test_issue_listing_survives_payload_shapes_the_models_reject() -> None:
    """The regression the raw-JSON read exists for, modeled on the observer's pulls fix:
    admission is the repo's front door, so ONE open issue whose payload the pinned models would
    reject (a rollout field nobody modelled, null in a typed-non-null spot, a ghost assignee)
    must cost at most itself — never stall admission for every other ticket in the repo."""
    poisoned = {
        "number": 45,
        "title": None,  # typed non-null in the models; tolerated as empty here
        "body": None,
        "state": "open",
        "labels": [{"name": "bug", "color": None}],
        "assignees": [None, {"login": "teammate"}],  # a ghost entry among real ones
        "sub_issues_summary": {"total": None},  # rollout shape nobody modelled yet
        "field_nobody_modelled_yet": {"shape": ["unknowable"]},
    }
    unnumbered = {"title": "alien entry with no number"}  # unadmittable: no external key
    candidates, _ = _candidates([[poisoned, unnumbered, _issue(46)]])

    issues = await candidates.open_issues("acme/widgets")

    assert [issue.number for issue in issues] == [45, 46]
    assert issues[0].title == ""
    assert issues[0].labels == frozenset({"bug"})
    assert issues[0].assignees == ("teammate",)


async def test_a_label_entry_of_an_alien_shape_is_dropped_not_fatal() -> None:
    """MINOR-3 guard (review of 7b16a45): a labels entry that is neither a bare string nor a
    label object must be dropped, not raise an AttributeError that takes the whole listing —
    and admission with it — hostage to one alien entry."""
    candidates, _ = _candidates([[_issue(1, labels=["adlc:new", 7, None, {"name": "bug"}])]])
    (issue,) = await candidates.open_issues("acme/widgets")
    assert issue.labels == frozenset({"adlc:new", "bug"})


async def test_a_non_integer_number_skips_that_entry_only_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """MINOR-3's other guard, plus MINOR-2's loudness: an entry whose `number` cannot be an int
    is unadmittable (no external key), so it is skipped — with a warning naming what was
    skipped, per the fail-loudly rule — while every other issue still admits."""
    alien = {"number": "forty-five", "title": "alien entry"}
    candidates, _ = _candidates([[_issue(1), alien, _issue(3)]])

    with caplog.at_level(logging.WARNING, logger="orchestrator.github.observer"):
        issues = await candidates.open_issues("acme/widgets")

    assert [issue.number for issue in issues] == [1, 3]
    assert any("forty-five" in record.getMessage() for record in caplog.records)


async def test_a_missing_number_is_skipped_loudly_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The unnumbered-entry skip must not be silent (MINOR 2): the same warning fires when the
    key is absent entirely."""
    candidates, _ = _candidates([[{"title": "no number at all"}, _issue(2)]])

    with caplog.at_level(logging.WARNING, logger="orchestrator.github.observer"):
        issues = await candidates.open_issues("acme/widgets")

    assert [issue.number for issue in issues] == [2]
    assert any("no usable issue number" in record.getMessage() for record in caplog.records)
