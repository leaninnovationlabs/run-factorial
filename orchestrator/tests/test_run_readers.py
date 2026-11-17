"""Every `step_run` reader sees only current runs (decision 76) unless it is named here as one
that reads history on purpose. A redo sets runs aside within a generation; a new reader that
forgets the rule would act on a run a person threw away, with no error anywhere."""

from __future__ import annotations

import ast
from typing import Final

from tests.paths import REPO_ROOT

SOURCE = REPO_ROOT / "orchestrator" / "src" / "orchestrator"

#: The current-run vocabulary: a reader that calls one of these sees no set-aside run.
_FILTERS = {"current_runs", "latest_current_run", "latest_run", "_runs_for"}

_SPEND = "spend and history count every run, set-aside ones included (deviation 10)"

#: Readers that see every run on purpose, as "<path under SOURCE>:<function>", with the reason.
HISTORY: Final[dict[str, str]] = {
    "worker/tick_context.py:_next_attempt": "attempt numbers keep counting (decision 67)",
    "worker/gates.py:_answered_review_ids": "an answered review stays answered",
    "worker/branches.py:unrecorded_runs": "an ended run's output record is history",
    "worker/previews.py:_latest_by_run": "preview lifecycle; a redo stops its own previews",
    "worker/loop.py:_sweep_project": (
        "keyed on live status; a redo ends every run it sets aside"
    ),
    "events/read_models.py:in_flight_runs": "live status, as above",
    "events/read_models.py:open_gates": "WAITING status, as above",
    "events/read_models.py:_not_sent_back": (
        "a predicate on an outer query that selects its own runs; it reads no run by itself"
    ),
    "events/terminals.py:terminal_admission": "one run by id",
    "api/deps.py:observe": "one run by id, for its ticket",
    "api/routes/runtimes.py:_last_auth_failures": "history",
    "api/routes/runtimes.py:_live_runs": (
        "keyed on live status and the ticket's generation; a redo ends every run it sets aside"
    ),
    "api/routes/tickets.py:_ticket_detail": "every run, each carrying superseded_at",
    "mcp_server/tools/tickets.py:_admitted": (
        "run history; its current run comes from refs.current_run"
    ),
    "events/read_models.py:_span_runs": _SPEND,
    "events/read_models.py:_ticket_estimated": _SPEND,
    "events/read_models.py:_ticket_runs": _SPEND,
    "events/read_models.py:_ticket_sum": _SPEND,
    "events/read_models.py:cost_per_ticket": _SPEND,
    "events/read_models.py:delivery_metrics": _SPEND,
    "events/read_models.py:done_ticket_cost": _SPEND,
    "events/read_models.py:project_spend": _SPEND,
    "events/read_models.py:run_extremes": _SPEND,
    "events/read_models.py:spend_by_outcome": _SPEND,
    "events/read_models.py:spend_by_ticket": _SPEND,
    "events/read_models.py:step_run_history": _SPEND,
    "events/read_models.py:ticket_step_costs": _SPEND,
    "events/read_models.py:time_split": _SPEND,
}


def _own_nodes(fn: ast.AST) -> list[ast.AST]:
    """`fn`'s body, nested function definitions excluded: each of those is its own reader."""
    found: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        found.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return found


def _readers() -> dict[str, ast.AST]:
    """Every function whose OWN body (nested defs excluded) names `StepRun.ticket_id`."""
    readers: dict[str, ast.AST] = {}
    for path in sorted(SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if any(
                isinstance(node, ast.Attribute)
                and node.attr == "ticket_id"
                and isinstance(node.value, ast.Name)
                and node.value.id == "StepRun"
                for node in _own_nodes(fn)
            ):
                readers[f"{path.relative_to(SOURCE).as_posix()}:{fn.name}"] = fn
    return readers


def _filters(fn: ast.AST) -> bool:
    """Its own body calls a name or attribute in _FILTERS."""
    for node in _own_nodes(fn):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
        if name in _FILTERS:
            return True
    return False


def test_every_run_reader_reads_current_runs_or_is_named_as_history() -> None:
    """A heuristic, not a proof: a function that calls one filter and also runs a separate
    unfiltered `StepRun` query passes."""
    assert sorted(n for n, fn in _readers().items() if not _filters(fn) and n not in HISTORY) == []


def test_the_history_list_names_only_readers_that_exist() -> None:
    assert sorted(set(HISTORY) - set(_readers())) == []
