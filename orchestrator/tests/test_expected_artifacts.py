"""Postcondition -> `RunSpec.expected_artifacts` derivation.

A pure function of one step and this ticket's template values, so it is tested as one: no
database, no GitHub, no substrate. `test_reconcile.py` covers the other half — that a real tick
puts what is derived here on the spec it hands the adapter.

The rule these cases pin down is that the derivation is deliberately NARROW. `.factorial/` gains no
key for declared delivery: the postconditions already say what a step must produce, and a second
place to say it would be a second thing to keep in sync. What cannot be derived is therefore
absent rather than approximated — `file_exists` is the standing example (a FILE, which no adapter
can verify). **COMMENT used to be absent for the same reason** — the closed predicate library had
no comment predicate — but it has two now (`issue_comment_contains`, `pr_comment_contains`), so
COMMENT is derivable; the cases below cover both.
"""

from __future__ import annotations

from typing import Any

from orchestrator.config import AgentStep
from orchestrator.worker.templates import _expected_artifacts
from runtime.contract import ArtifactKind


def _agent_step(postconditions: Any) -> AgentStep:
    """The plan step's shape, with only its postconditions varied."""
    return AgentStep.model_validate(
        {
            "name": "plan",
            "kind": "agent",
            "agent": "plan-agent",
            "next": "spec_gate",
            "workspace": {"repo": "{repo}", "base": "main", "branch": "spec/{ticket}-{slug}"},
            "postconditions": postconditions,
        }
    )


def test_only_conjunctive_predicates_become_expectations() -> None:
    """A call under `any:` is one alternative among several and a call under `not:` asserts the
    opposite. Reading either as "must be delivered" would fail runs that satisfied the
    postconditions perfectly well."""
    step = _agent_step(
        {
            "all": [
                'branch_exists("spec/{ticket}-*")',
                {"any": ['pr_open("spec/{ticket}-*", "main")', 'ci_green("spec/{ticket}-*")']},
                {"not": 'branch_exists("wip/{ticket}-*")'},
            ]
        }
    )
    assert [
        (item.kind, dict(item.selector)) for item in _expected_artifacts(step, {"ticket": "12"})
    ] == [(ArtifactKind.BRANCH, {"head_glob": "spec/12-*"})]


def test_predicates_that_name_no_delivery_derive_nothing() -> None:
    """`file_exists` in particular: it would derive a FILE, which no adapter can verify — every
    spawn of the canonical plan step would then be refused. The postconditions still check it;
    only the delivery probe narrows."""
    step = _agent_step(
        {
            "all": [
                'file_exists("specs/{ticket}-*/spec.md", "spec/{ticket}-*")',
                'ci_green("spec/{ticket}-*")',
                'ticket_label("adlc:plan")',
                'pr_merged("spec/{ticket}-*", "main")',
            ]
        }
    )
    assert _expected_artifacts(step, {"ticket": "12"}) == ()


def test_a_step_with_no_postconditions_declares_nothing() -> None:
    """Which leaves the adapter's own default probe in charge."""
    assert _expected_artifacts(_agent_step(None), {"ticket": "12"}) == ()


def test_the_same_glob_twice_is_one_expectation_per_kind() -> None:
    step = _agent_step(
        {
            "all": [
                'branch_exists("spec/{ticket}-*")',
                'branch_exists("spec/{ticket}-*")',
                'pr_open("spec/{ticket}-*", "main")',
            ]
        }
    )
    assert len(_expected_artifacts(step, {"ticket": "12"})) == 2


def test_a_pr_expectation_drops_the_base_deliberately() -> None:
    """`pr_open`'s second argument is the acceptance gate's business — the orchestrator still
    evaluates the predicate in full. The expectation is only the substrate's delivery probe, and
    `base` is not in the contract's selector vocabulary."""
    (expectation,) = _expected_artifacts(
        _agent_step('pr_open("impl/{ticket}-*", "main")'), {"ticket": "12"}
    )
    assert dict(expectation.selector) == {"head_glob": "impl/12-*"}


def test_a_slug_placeholder_becomes_a_wildcard() -> None:
    """`{slug}` is the agent's own choice and no value for it exists at spawn. Carried through as
    written it would be a glob nothing can match — a guaranteed false `failed` on a run that
    delivered — so it gets the same treatment every adapter gives `workspace.branch`."""
    (expectation,) = _expected_artifacts(
        _agent_step('branch_exists("spec/{ticket}-{slug}")'), {"ticket": "12"}
    )
    assert dict(expectation.selector) == {"head_glob": "spec/12-*"}


def test_a_glob_that_already_spells_the_wildcard_is_untouched() -> None:
    """The canonical form in `docs/samples/factorial-simple/processes/process-simple.yaml`."""
    (expectation,) = _expected_artifacts(
        _agent_step('branch_exists("spec/{ticket}-*")'), {"ticket": "12"}
    )
    assert dict(expectation.selector) == {"head_glob": "spec/12-*"}


def test_a_resolved_slug_leaves_nothing_for_globbed_to_wildcard() -> None:
    """Once `worker/templates._resolve_slug` has put a concrete `slug` in `tick.values`,
    `expand` resolves `{slug}` exactly like `{ticket}` — `spec/{ticket}-{slug}` expands FULLY, so
    `globbed` (which only replaces placeholders still standing after `expand`) has nothing left
    to turn into `*`. The derived expectation is the concrete branch name, not a wildcard glob."""
    (expectation,) = _expected_artifacts(
        _agent_step('branch_exists("spec/{ticket}-{slug}")'),
        {"ticket": "12", "slug": "widget"},
    )
    assert dict(expectation.selector) == {"head_glob": "spec/12-widget"}


# --- COMMENT ----------------------------------------------------------------------------------


def test_issue_comment_contains_derives_an_issue_comment_expectation() -> None:
    (expectation,) = _expected_artifacts(
        _agent_step('issue_comment_contains("Evidence Pack")'), {"ticket": "12"}
    )
    assert expectation.kind == ArtifactKind.COMMENT
    assert dict(expectation.selector) == {"marker": "Evidence Pack"}


def test_pr_comment_contains_derives_a_pr_comment_expectation() -> None:
    (expectation,) = _expected_artifacts(
        _agent_step('pr_comment_contains("impl/{ticket}-*", "Evidence Pack")'), {"ticket": "12"}
    )
    assert expectation.kind == ArtifactKind.COMMENT
    assert dict(expectation.selector) == {
        "marker": "Evidence Pack",
        "where": "pr",
        "head_glob": "impl/12-*",
    }


def test_pr_comment_contains_globs_a_slug_placeholder_in_its_head_glob() -> None:
    """Same treatment `branch_exists`/`pr_open` get: `{slug}` is the agent's own choice, so it
    becomes `*` in the derived `head_glob` rather than travelling as written."""
    (expectation,) = _expected_artifacts(
        _agent_step('pr_comment_contains("impl/{ticket}-{slug}", "Evidence Pack")'),
        {"ticket": "12"},
    )
    assert dict(expectation.selector)["head_glob"] == "impl/12-*"


def test_a_marker_is_expanded_but_never_globbed() -> None:
    """A marker is matched as a literal substring, never an `fnmatch` glob — an (unlikely)
    placeholder left standing in one must not turn into a `*` nothing would then search for."""
    (expectation,) = _expected_artifacts(
        _agent_step('issue_comment_contains("Evidence Pack {ticket}")'), {"ticket": "12"}
    )
    assert dict(expectation.selector) == {"marker": "Evidence Pack 12"}


def test_two_comment_expectations_with_different_markers_both_survive() -> None:
    """The dedup key is the whole selector, not just a glob: two COMMENT expectations that only
    differ by marker are two distinct deliveries this run must make, not duplicates of each
    other."""
    step = _agent_step(
        {
            "all": [
                'issue_comment_contains("Evidence Pack: plan")',
                'issue_comment_contains("Evidence Pack: review")',
            ]
        }
    )
    assert len(_expected_artifacts(step, {"ticket": "12"})) == 2


def test_the_same_comment_expectation_twice_is_one() -> None:
    step = _agent_step(
        {
            "all": [
                'issue_comment_contains("Evidence Pack")',
                'issue_comment_contains("Evidence Pack")',
            ]
        }
    )
    assert len(_expected_artifacts(step, {"ticket": "12"})) == 1


def test_a_comment_predicate_under_any_derives_nothing() -> None:
    """The conjunctive-only rule (`test_only_conjunctive_predicates_become_expectations`) holds
    for comment predicates exactly as it does for `branch_exists`/`pr_open`."""
    step = _agent_step(
        {"any": ['issue_comment_contains("Evidence Pack")', 'ticket_label("adlc:done")']}
    )
    assert _expected_artifacts(step, {"ticket": "12"}) == ()


def test_a_marker_that_expands_to_empty_derives_nothing_and_raises_nothing() -> None:
    """`{ticket_owner}` is a legal marker at config-parse time — `PredicateCall` validation only
    ever sees the unexpanded source text (`config/conditions.py`) — but resolves to `""` for a
    ticket admitted with no assignee (`owner_at_admission == ""`, `worker/admission.py`). Building
    an `ExpectedArtifact` from that empty marker would raise `ExpectedArtifact`'s own non-empty
    check, and it would do so on EVERY spawn attempt of the step rather than once: this must not
    happen. `_expected_artifacts` derives nothing for the call instead, silently, so the step's
    postcondition (which itself reads false for an empty marker) fails through the ordinary
    retry/escalate machinery rather than wedging the ticket via a repeating exception."""
    step = _agent_step('issue_comment_contains("{ticket_owner}")')
    assert _expected_artifacts(step, {"ticket": "12", "ticket_owner": ""}) == ()


def test_a_pr_comment_marker_that_expands_to_empty_also_derives_nothing() -> None:
    step = _agent_step('pr_comment_contains("impl/{ticket}-*", "{ticket_owner}")')
    assert _expected_artifacts(step, {"ticket": "12", "ticket_owner": ""}) == ()
