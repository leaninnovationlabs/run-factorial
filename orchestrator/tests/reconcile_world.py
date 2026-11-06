"""The shared world for the reconcile test family: process-config builders, the test
doubles, `World` and its builder, and the tick/query helpers every section uses.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Final
from uuid import uuid4

from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.expression import text as sql_text

from orchestrator.api.services.organizations import default_organization_id
from orchestrator.config import AgentManifest, McpServerManifest, ProcessConfig
from orchestrator.db.models import (
    Event,
    ProcessVersion,
    Project,
    Runtime,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketSource,
    TicketSourceKind,
    TicketStatus,
)
from orchestrator.events import EventType, append
from orchestrator.factorial import (
    DEFAULT_KEY,
    ResolvedAgent,
    ResolvedMcpServer,
    ResolvedSkill,
)
from orchestrator.github import (
    BLOCKED_LABELS,
    LABEL_EXIT,
    BranchState,
    CheckRunState,
    IssueState,
    MergeRefused,
    PullRequestState,
    SnapshotState,
    StateSource,
    TreeEntry,
)
from orchestrator.github.client import GitHubClient, RunToken
from orchestrator.github.contents import ComparedFile, Comparison, RepoContents
from orchestrator.github.media import Fetched, ItemRef, MediaRef, Unavailable, media_refs
from orchestrator.github.state import CommentState, RepoPermission, ReviewState, ReviewVerdict
from orchestrator.secrets.store import put
from orchestrator.worker.context import ProjectGitHub, ReconcileDeps, SessionPolicy
from orchestrator.worker.parked import ParkedListings
from orchestrator.worker.reconcile import reconcile_ticket
from orchestrator.worker.runtime_adapters import RuntimeAdapters, load_runtime_by_slug
from runtime.contract import (
    IdleReason,
    InspectPhase,
    InspectState,
    MissingAttachment,
    ParkReason,
    PreviewPhase,
    PreviewSpec,
    PreviewState,
    PreviewUrl,
    RunAttachment,
    RunHandle,
    RunResult,
    RunSpec,
    RunStatus,
    SessionState,
    SessionUnavailable,
    TerminalTarget,
    UnknownRunError,
)

START = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
#: The App's own bot login, for the comment predicates' and comment-inputs' author fence.
BOT_LOGIN = "sentinel[bot]"


# --------------------------------------------------------------------------- the process


def _process(name: str) -> ProcessConfig:
    """A two-step process shaped like the canonical sample's `plan`/`spec_gate` pair — a
    deliberately smaller slice; the real sample has five steps. `plan`'s own precondition here is
    just `ticket_has_assignee()`, no `not: branch_exists(...)` guard, the same as the real sample:
    the guard blocks any second attempt, and a rework loop needs to be testable."""
    return ProcessConfig.model_validate(
        {
            "specVersion": "factorial/v1",
            "kind": "Process",
            "name": name,
            "version": 1,
            "roles": {"reviewers": ["reviewer-1"]},
            "defaults": {
                "timeout": "1h",
                "retry": {"maxAttempts": 2, "backoff": "10m"},
                "onFailure": {
                    "escalate": {
                        "ticketLabel": "factorial:blocked",
                        "assign": ["{ticket_owner}"],
                        "comment": "ADLC {step} failed after {attempt} attempt(s): "
                        "{failure_reason}.",
                    }
                },
            },
            "entry": {
                "when": {"all": ['ticket_label("factorial:new")', "ticket_has_assignee()"]},
                "step": "plan",
            },
            "steps": [
                {
                    "name": "plan",
                    "kind": "agent",
                    "agent": "plan-agent",
                    "next": "spec_gate",
                    "workspace": {
                        "repo": "{repo}",
                        "base": "main",
                        "branch": "spec/{ticket}-{slug}",
                    },
                    "preconditions": "ticket_has_assignee()",
                    "inputs": ["ticket_body"],
                    "postconditions": {
                        "all": [
                            'branch_exists("spec/{ticket}-*")',
                            'pr_open("spec/{ticket}-*", "main")',
                        ]
                    },
                    "onSuccess": {
                        "ticketLabel": "factorial:spec-review",
                        "assign": ["{roles.reviewers}"],
                        "comment": "Spec ready for review: {pr_url}",
                    },
                },
                {
                    "name": "spec_gate",
                    "kind": "human_gate",
                    "next": "done",
                    "gate": {
                        "type": "pr_approval",
                        "pr": {"head": "spec/{ticket}-*", "base": "main"},
                        "approvers": ["{roles.reviewers}"],
                        "waiting": {"ticketLabel": "factorial:spec-review"},
                        "onApprove": {
                            "merge": True,
                            "ticketLabel": "factorial:done",
                            "assign": ["{ticket_owner}"],
                        },
                        "onReject": {
                            "rerunStep": "plan",
                            "ticketLabel": "factorial:spec-rework",
                            "assign": ["{ticket_owner}"],
                        },
                    },
                },
            ],
            "terminal": {
                "step": "done",
                "actions": {"closeTicket": True, "comment": "ADLC complete — merged to main."},
            },
        }
    )


def _process_with_comment_inputs(
    name: str,
    *,
    plan_inputs: tuple[str, ...] = ("ticket_body", "ticket_comments", "pr_review_comments"),
    gate_reject_inputs: tuple[str, ...] = (),
) -> ProcessConfig:
    """`_process()`, with the plan step's own `inputs:` and/or the gate's `onReject.inputs`
    swapped out — everything else (postconditions, roles, retry) is the byte-for-byte
    same fixture the rest of this module exercises."""
    cfg = _process(name).model_dump(mode="json", by_alias=True)
    cfg["steps"][0]["inputs"] = list(plan_inputs)
    if gate_reject_inputs:
        cfg["steps"][1]["gate"]["onReject"]["inputs"] = list(gate_reject_inputs)
    return ProcessConfig.model_validate(cfg)


def _process_with_open_approvers(name: str) -> ProcessConfig:
    """`_process()` with `spec_gate.approvers` emptied — "any login's review counts" (`Gate`'s
    own default), so an approval and an unrelated changes-requested review can sit on the SAME PR
    from two DIFFERENT logins without needing a two-member role."""
    cfg = _process(name).model_dump(mode="json", by_alias=True)
    cfg["steps"][1]["gate"]["approvers"] = []
    return ProcessConfig.model_validate(cfg)


def _process_with_gate(
    name: str, *, base: ProcessConfig | None = None, **gate: Any
) -> ProcessConfig:
    """`_process()` (or `base`) with `spec_gate.gate` keys replaced, spelled as YAML spells
    them: `approvers=[...]`, `approverPermission="write"`."""
    cfg = (base or _process(name)).model_dump(mode="json", by_alias=True)
    cfg["steps"][1]["gate"].update(gate)
    return ProcessConfig.model_validate(cfg)


def _legacy_process(name: str) -> ProcessConfig:
    """`_process()` as a snapshot pinned before issue #138: `adlc/v1` and `adlc:` labels. A pinned
    snapshot re-parses on every tick for ever (decision 32), so one drive keeps this spelling."""
    text = json.dumps(_process(name).model_dump(mode="json", by_alias=True))
    return ProcessConfig.model_validate(
        json.loads(text.replace("factorial/v1", "adlc/v1").replace('"factorial:', '"adlc:'))
    )


def _process_without_gate_reject(name: str) -> ProcessConfig:
    """`_process()` with `spec_gate.onReject` removed — a rejection has nowhere to loop back to,
    so it blocks (`_reject`'s no-onReject arm) instead of rerunning `plan`. The gate-blocked
    un-block case needs exactly this shape: a BLOCKED ticket whose `current_step` is still the
    gate, not `plan`."""
    cfg = _process(name).model_dump(mode="json", by_alias=True)
    del cfg["steps"][1]["gate"]["onReject"]
    return ProcessConfig.model_validate(cfg)


def _process_without_gate_reject_or_escalate(name: str) -> ProcessConfig:
    """`_process_without_gate_reject()`, with `defaults.onFailure` ALSO removed — the absence
    case: no declared escalate outcome for `_reject`'s no-onReject arm to apply, so the arm must
    stay label-only (never invent comment/assign text `defaults.onFailure` does not declare)."""
    cfg = _process_without_gate_reject(name).model_dump(mode="json", by_alias=True)
    del cfg["defaults"]["onFailure"]
    return ProcessConfig.model_validate(cfg)


def _process_without_gate_reject_and_escalate_label(name: str) -> ProcessConfig:
    """`_process_without_gate_reject()`, with `defaults.onFailure.escalate.ticketLabel` ALSO
    removed — an escalate outcome that still posts a comment and reassigns, but declares NO
    label of its own. `_reject`'s no-onReject arm must still force `factorial:blocked` here even
    when it runs in the SAME TICK as `_open_gate` (which may already have set `tick.plan.label`
    to `gate.waiting`'s own label moments earlier) — this is the fixture that shape needs, since
    `_process_without_gate_reject`'s OWN escalate declares `ticketLabel: "factorial:blocked"`
    and so never exercises the gap (`_apply_outcome` already overwrites `tick.plan.label`
    unconditionally whenever the outcome itself declares one)."""
    cfg = _process_without_gate_reject(name).model_dump(mode="json", by_alias=True)
    del cfg["defaults"]["onFailure"]["escalate"]["ticketLabel"]
    return ProcessConfig.model_validate(cfg)


def _process_with_spec_merge_gate(name: str) -> ProcessConfig:
    """`_process_with_test_and_code_gate()`, extended with a SECOND human gate after `code_gate`
    — the real sandbox lane's step ORDERING and gate-2 shape:
    `code_gate` approves without merging, and
    `spec_merge_gate` — gating the SAME pull request, but requiring a DIFFERENT approver, so the
    standing `code_gate` approval never auto-satisfies it — carries NO `onReject`, matching the
    sandbox's real `spec_merge_gate`'s no-rework-edge shape. The PR stack itself is SIMPLIFIED,
    not a faithful copy: the real lane stacks a spec PR and an impl PR (`code_gate` merges impl
    into spec, `spec_merge_gate` merges spec into `main`); this fixture reuses
    `_process_with_test_and_code_gate`'s single impl-PR-to-`main` shape for both gates rather
    than modeling that second branch. Exists for the end-to-end proof that the no-onReject arm's
    escalate outcome fires on a multi-gate shape, not only on the smaller two-step fixture."""
    cfg = _process_with_test_and_code_gate(name).model_dump(mode="json", by_alias=True)
    code_gate = cfg["steps"][4]
    assert code_gate["name"] == "code_gate"
    code_gate["gate"]["onApprove"]["merge"] = False
    code_gate["next"] = "spec_merge_gate"
    cfg["steps"].append(
        {
            "name": "spec_merge_gate",
            "kind": "human_gate",
            "next": "done",
            "gate": {
                "type": "pr_approval",
                "pr": {"head": "impl/{ticket}-*", "base": "main"},
                "approvers": ["reviewer-2"],
                "waiting": {"ticketLabel": "factorial:spec-merge"},
                "onApprove": {"merge": True, "ticketLabel": "factorial:done"},
                # NO onReject, deliberately — the sandbox's gate 2.
            },
        }
    )
    return ProcessConfig.model_validate(cfg)


def _process_without_retry(name: str) -> ProcessConfig:
    """`_process()` with `defaults.retry` removed and no per-step override — `_grant_attempt`'s
    Path A: a process config with NO retry policy at all (this parses fine; `Retry` is optional
    on both `Defaults` and `AgentStep`/`ActionStep`). `_agent_step`'s FAILED branch can then never
    grant a new attempt (`retry is not None` is false forever), so `_grant_attempt` must refuse
    rather than let the ticket sit RUNNING in silence."""
    cfg = _process(name).model_dump(mode="json", by_alias=True)
    del cfg["defaults"]["retry"]
    return ProcessConfig.model_validate(cfg)


def _process_with_a_branch_guard(name: str) -> ProcessConfig:
    """`_process()` with `plan`'s preconditions extended to also require
    `not: branch_exists("spec/{ticket}-*")` — the shape the REAL sample carried until it retired
    the guard (the precondition-site comment in
    `docs/samples/factorial-simple/processes/process-simple.yaml` has the full trace of why). Kept
    here on purpose even though no shipped config carries this guard any more: `_grant_attempt`'s
    Path B is real, general machinery — a precondition that can only become true again after a
    human acts elsewhere — and this fixture is what exercises it. Here, once a failed attempt has
    already pushed a branch, this precondition can only become true again after a human deletes
    it — `_start_attempt`'s precondition guard declines silently forever until then."""
    cfg = _process(name).model_dump(mode="json", by_alias=True)
    cfg["steps"][0]["preconditions"] = {
        "all": [{"not": 'branch_exists("spec/{ticket}-*")'}, "ticket_has_assignee()"]
    }
    return ProcessConfig.model_validate(cfg)


def _process_with_a_slug_precondition(name: str) -> ProcessConfig:
    """`_process()`, with `spec_gate.next` sent to a third agent step (`confirm`) whose OWN
    `preconditions` reference `{slug}` directly — `branch_exists("spec/{ticket}-{slug}")` —
    rather than the canonical `*`-globbed form. Exists only to prove that once `_resolve_slug`
    puts a concrete `slug` in `tick.values`, `predicates.evaluator.
    _evaluate_call`'s existing `expand(arg, ctx.values)` substitutes it into ANY predicate
    argument, not just `workspace.branch` — so a predicate written this way evaluates CONCRETELY
    (an exact `matches_ref` comparison — `predicates/globs.py`'s no-`*`-means-literal branch)
    once the ticket's slug is on record, with no evaluator change at all.

    This precondition could never be `plan`'s own: on attempt 1 no artifact exists yet, so
    `{slug}` cannot resolve and the literal pattern (`"spec/12-{slug}"`, no `*`) could never
    exact-match a real branch — a step's own delivery can never gate its own first spawn. It sits
    on a step reached only AFTER `plan` has already delivered, so the artifact predates it."""
    cfg = _process(name).model_dump(mode="json", by_alias=True)
    cfg["steps"][1]["next"] = "confirm"
    cfg["steps"].append(
        {
            "name": "confirm",
            "kind": "agent",
            "agent": "plan-agent",
            "next": "done",
            "workspace": {"repo": "{repo}", "base": "main", "branch": "confirm/{ticket}"},
            "preconditions": 'branch_exists("spec/{ticket}-{slug}")',
        }
    )
    return ProcessConfig.model_validate(cfg)


def _process_with_a_build_step(name: str) -> ProcessConfig:
    """`_process()`, with `spec_gate.next` sent to a `build` agent step whose own
    `workspace.branch` is a SECOND `{slug}`-bearing prefix (`impl/{ticket}-{slug}`, distinct
    from `plan`'s `spec/{ticket}-{slug}`) — the canonical sample shape (design.md's own
    plan/build split). `build`'s postconditions are star-globbed (`impl/{ticket}-*`, never
    `{slug}`), exactly like the real sample: build accepts WHATEVER slug its own agent picked,
    independent of any earlier step's. `build.onSuccess.comment` carries `{pr_url}`, rendered
    once and permanently by `_apply_outcome` on the same tick `_accept` advances — the exact
    human-visible surface a regression proved: `_glob` must never consume the ticket-level
    resolved slug when it looks up which PR `{pr_url}` means, because build's agent is free to
    choose a slug that disagrees with `plan`'s."""
    cfg = _process(name).model_dump(mode="json", by_alias=True)
    cfg["steps"][1]["next"] = "build"
    cfg["steps"].append(
        {
            "name": "build",
            "kind": "agent",
            "agent": "plan-agent",
            "next": "done",
            "workspace": {"repo": "{repo}", "base": "main", "branch": "impl/{ticket}-{slug}"},
            "postconditions": {
                "all": [
                    'branch_exists("impl/{ticket}-*")',
                    'pr_open("impl/{ticket}-*", "main")',
                ]
            },
            "onSuccess": {"comment": "Implementation ready for testing: {pr_url}"},
        }
    )
    return ProcessConfig.model_validate(cfg)


def _process_with_test_and_code_gate(name: str) -> ProcessConfig:
    """`_process_with_a_build_step()`, extended with the sample's own `test` -> `code_gate` pair
    — the FULL canonical graph (plan -> spec_gate -> build -> test -> code_gate -> done), and
    `build` gains the sample's own `pr_merged("spec/{ticket}-*", "main")`
    precondition (absent from the smaller fixture, which does not need it). `code_gate.onReject`
    also carries the sample's own `inputs: [pr_review_comments]`, matching the fixture to the
    real config's SECOND gate — every other rework-input test in this module exercises only the
    FIRST gate's rejection (`spec_gate` -> `plan`), which never proves `_rework_inputs`'s own
    config scan (`worker/inputs.py`) picks the RIGHT gate out of two when it walks
    `tick.config.steps` looking for the one whose `onReject.rerunStep` names the step at hand.
    Exists for the live rework proof: `code_gate.onReject` reruns `build`, whose `next` is
    `test`, so a genuine rework has to reach `test` and `code_gate` a second time, not just spawn
    once and stop — `test_build_rework_reaches_test_and_code_gate_again` is the test that needs
    this shape, and it is also what proves the second-gate `_rework_inputs` scan."""
    cfg = _process_with_a_build_step(name).model_dump(mode="json", by_alias=True)
    build_step = cfg["steps"][2]
    assert build_step["name"] == "build"
    build_step["next"] = "test"
    build_step["preconditions"] = {"all": ['pr_merged("spec/{ticket}-*", "main")']}
    build_step["onSuccess"] = {
        "ticketLabel": "factorial:testing",
        "comment": "Implementation ready for testing: {pr_url}",
    }
    cfg["steps"].append(
        {
            "name": "test",
            "kind": "agent",
            "agent": "plan-agent",
            "next": "code_gate",
            "workspace": {"repo": "{repo}", "base": "main", "branch": "impl/{ticket}-{slug}"},
            "preconditions": {"all": ['pr_open("impl/{ticket}-*", "main")']},
            "postconditions": {
                "all": [
                    'ci_green("impl/{ticket}-*")',
                    'pr_comment_contains("impl/{ticket}-*", "## Test Verdict")',
                ]
            },
            "onSuccess": {
                "ticketLabel": "factorial:code-review",
                "comment": "Implementation ready for review: {pr_url}",
            },
        }
    )
    cfg["steps"].append(
        {
            "name": "code_gate",
            "kind": "human_gate",
            "next": "done",
            "gate": {
                "type": "pr_approval",
                "pr": {"head": "impl/{ticket}-*", "base": "main"},
                "approvers": ["reviewer-1"],
                "waiting": {"ticketLabel": "factorial:code-review"},
                "onApprove": {"merge": True, "ticketLabel": "factorial:done"},
                "onReject": {
                    "rerunStep": "build",
                    "inputs": ["pr_review_comments"],
                    "ticketLabel": "factorial:build-rework",
                },
            },
        }
    )
    return ProcessConfig.model_validate(cfg)


def _process_with_action(name: str) -> ProcessConfig:
    """A one-step process whose only step is `kind: action` — entry lands straight on
    it, and its own `next` is the terminal step. Independent of `_process()`'s plan/gate shape:
    action-step tests need none of that machinery, only the dispatch and the retry/escalate path
    it shares with agent steps."""
    return ProcessConfig.model_validate(
        {
            "specVersion": "factorial/v1",
            "kind": "Process",
            "name": name,
            "version": 1,
            "defaults": {
                "retry": {"maxAttempts": 2, "backoff": "10m"},
                "onFailure": {
                    "escalate": {
                        "ticketLabel": "factorial:blocked",
                        "assign": ["{ticket_owner}"],
                        "comment": "ADLC {step} failed after {attempt} attempt(s): "
                        "{failure_reason}.",
                    }
                },
            },
            "entry": {"when": 'ticket_label("factorial:new")', "step": "fix"},
            "steps": [
                {
                    "name": "fix",
                    "kind": "action",
                    "action": "fix_the_contract",
                    "next": "done",
                    "onSuccess": {
                        "ticketLabel": "factorial:fixed",
                        "comment": "Contract fixed at {contract_sha}.",
                    },
                },
            ],
            "terminal": {"step": "done", "actions": {"closeTicket": True}},
        }
    )


def _process_with_check(name: str) -> ProcessConfig:
    """A one-step process whose only step is `kind: check` — entry lands straight on it, its
    `next` is the terminal step, and its shape is the CI-validation stage the kind exists for:
    ci_green plus pr_open as conditions, a `pr:` matcher for `{pr_url}`, and a per-step timeout
    shorter than the default so the deadline is provably the step's own."""
    return ProcessConfig.model_validate(
        {
            "specVersion": "factorial/v1",
            "kind": "Process",
            "name": name,
            "version": 1,
            "defaults": {
                "retry": {"maxAttempts": 2, "backoff": "10m"},
                "onFailure": {
                    "escalate": {
                        "ticketLabel": "factorial:blocked",
                        "assign": ["{ticket_owner}"],
                        "comment": "ADLC {step} failed after {attempt} attempt(s): "
                        "{failure_reason}.",
                    }
                },
            },
            "entry": {"when": 'ticket_label("factorial:new")', "step": "validate_ci"},
            "steps": [
                {
                    "name": "validate_ci",
                    "kind": "check",
                    "next": "done",
                    "conditions": {
                        "all": [
                            'ci_green("impl/{ticket}-*")',
                            'pr_open("impl/{ticket}-*", "spec/{ticket}-*")',
                        ]
                    },
                    "pr": {"head": "impl/{ticket}-*", "base": "spec/{ticket}-*"},
                    "timeout": "30m",
                    "onSuccess": {
                        "ticketLabel": "factorial:code-review",
                        "comment": "CI green: {pr_url}",
                    },
                },
            ],
            "terminal": {"step": "done", "actions": {"closeTicket": True}},
        }
    )


def _process_on_runtime(name: str, slug: str) -> ProcessConfig:
    """`_process()` pinned to a registry runtime by `runtime: <slug>` — the one key the registry
    adds to a process. Everything else is the same fixture, so a test that passes this config
    proves the slug alone changed where the run went."""
    cfg = _process(name).model_dump(mode="json", by_alias=True)
    cfg["runtime"] = slug
    return ProcessConfig.model_validate(cfg)


def _runtime_slug() -> str:
    """Unique per call: `runtime.slug` is UNIQUE and the test database is never truncated."""
    return f"rt-{uuid4().hex[:10]}"


MANIFEST = AgentManifest.model_validate(
    {
        "specVersion": "factorial/v1",
        "kind": "Agent",
        "name": "plan-agent",
        "prompts": {"system": "system-prompt.md", "instructions": "instructions.md"},
        "model": "claude-haiku-4-5-20251001",
        "limits": {"max_duration": "1h", "max_turns": 40},
        "secrets": [{"key": "GH_TOKEN"}],
        "runtime": {"kind": "claude"},
    }
)

#: What `orchestrator.factorial` hands the worker: the manifest plus every file it names, at one
#: sha. The loop only ever sees contents, so the fake carries contents too.
#: Already checked against the project's operator allowlist by `factorial.registry` — by the
#: time the
#: worker sees a resolved agent, permission is settled (design.md §7.5).
MCP_SERVER = ResolvedMcpServer(
    name="product-docs",
    scope="shared",
    manifest=McpServerManifest.model_validate(
        {
            "specVersion": "factorial/v1",
            "kind": "McpServer",
            "name": "product-docs",
            "transport": "http",
            "http": {
                "url": "https://mcp.internal.example.com/product-docs/mcp",
                "headers": {"Authorization": "Bearer {secrets.PRODUCT_DOCS_TOKEN}"},
            },
            "secrets": [{"key": "PRODUCT_DOCS_TOKEN"}],
            "tools": {"allow": ["search_docs"]},
        }
    ),
)

RESOLVED_AGENT = ResolvedAgent(
    name=MANIFEST.name,
    repo="acme/widgets",
    commit_sha="0" * 40,
    manifest=MANIFEST,
    system_prompt="You are the plan agent.",
    instructions="Write a spec, push a branch, open a PR.",
    skills=(ResolvedSkill(name="spec-format", content="# Spec format\n", scope="shared"),),
    mcp_servers=(MCP_SERVER,),
)


# --------------------------------------------------------------------------- test doubles


class _FakeRequestFailed(Exception):
    """Stands in for `githubkit.exception.RequestFailed` (confirmed via its own MRO: a plain
    `Exception`, never a `RuntimeError`) — the infrastructure-class tests need an exception
    that is NOT one of `failures._PERMANENT_CONFIG_ERRORS`, to prove the classification wrap
    does not overreach into a genuinely transient failure."""


#: Enough of a PNG for the contract: a digest that matches and a PNG's magic bytes.
PNG_BYTES: Final = b"\x89PNG\r\n\x1a\n" + b"world-image"


@dataclass
class FakeMedia:
    """`MediaSource` over literals: what GitHub would render and serve, by asset key. An asset
    it holds no outcome for resolves to None — named in text, embedded nowhere."""

    outcomes: dict[str, Fetched | Unavailable] = field(default_factory=dict)
    calls: list[tuple[tuple[ItemRef, MediaRef], ...]] = field(default_factory=list)
    #: Raised by `resolve`, as GitHub being unable to serve an image would be.
    error: Exception | None = None

    async def resolve(
        self, wanted: Sequence[tuple[ItemRef, MediaRef]]
    ) -> tuple[Fetched | Unavailable | None, ...]:
        self.calls.append(tuple(wanted))
        if self.error is not None:
            raise self.error
        return tuple(self.outcomes.get(ref.asset) for _, ref in wanted)

    def image(self, uuid: str, content: bytes = PNG_BYTES) -> str:
        """Serve a PNG for `uuid`; returns the link an author would paste."""
        link = f"https://github.com/user-attachments/assets/{uuid}"
        (ref,) = media_refs(link)
        self.outcomes[ref.asset] = Fetched(
            ref, "image/png", content, hashlib.sha256(content).hexdigest()
        )
        return link

    def unavailable(self, uuid: str, reason: str) -> str:
        link = f"https://github.com/user-attachments/assets/{uuid}"
        (ref,) = media_refs(link)
        self.outcomes[ref.asset] = Unavailable(ref, reason)
        return link


class Clock:
    """Injectable time. Tests move it; nothing sleeps."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@dataclass
class FakeGitHub:
    """Mutable repo state. `snapshot()` freezes it the way a real poll would."""

    repo: str
    issue: IssueState
    branches: list[BranchState] = field(default_factory=list)
    pulls: list[PullRequestState] = field(default_factory=list)
    issue_comments: list[CommentState] = field(default_factory=list)
    pr_comments: dict[int, list[CommentState]] = field(default_factory=dict)
    #: Every review per PR number, oldest first — the contract `pr_reviews()` keeps for real.
    pr_reviews: dict[int, list[ReviewState]] = field(default_factory=dict)
    review_count: int = 0
    comment_count: int = 0
    #: `PR number -> its verdict` — off the PR listing because review observation is lazy, keyed
    #: exactly like `SnapshotState.review_verdicts_by_number`. `approve`/
    #: `request_changes` write here; a test that seeds a PR pre-approved writes here directly.
    verdicts: dict[int, ReviewVerdict] = field(default_factory=dict)
    #: `login -> the repository role GitHub reports`; a login not listed has none.
    permissions: dict[str, RepoPermission] = field(default_factory=dict)
    #: Every permission lookup, in order — what the cost assertions count.
    permission_reads: list[str] = field(default_factory=list)
    #: `sha -> check runs` — `ci_green`'s own data source (`predicates.library.ci_green`), keyed
    #: the same way `SnapshotState.check_runs_by_sha` is.
    check_runs_by_sha: dict[str, tuple[CheckRunState, ...]] = field(default_factory=dict)
    #: `tag name -> sha`. Read/written by `RecordingContractGitHub` — `fix_the_contract`'s own
    #: mutation surface, not `StateSource`'s (a git tag is not something predicates read).
    tags: dict[str, str] = field(default_factory=dict)
    #: Per-ticket issue reads this fake has answered — what decision 30 exists to hold at zero
    #: for a parked ticket. Counted on the snapshot's `issue()`, not on `snapshot()` itself,
    #: because building the source is free and only the read costs GitHub anything.
    issue_reads: int = 0
    #: Snapshots that read anything but the issue: a parked re-check's cost, one per tick.
    repo_reads: int = 0
    #: `labelled_issues` calls, as (repo, label), so a test can prove one listing per tick.
    listing_calls: list[tuple[str, str]] = field(default_factory=list)
    #: When True the listing fails this tick, the way GitHub does.
    listing_unavailable: bool = False
    #: What the parked listing reports for this issue when it should differ from the issue the
    #: per-ticket read returns — a listing taken earlier in a long tick.
    listing_issue: IssueState | None = None
    #: `commit -> blobs`, what `RepoContents.tree` would list (tech-stack decision 58).
    trees: dict[str, tuple[TreeEntry, ...]] = field(default_factory=dict)
    tree_error: Exception | None = None
    tree_calls: list[str] = field(default_factory=list)
    #: `(base, head) -> Comparison`, what `RepoContents.compare` answers (tech-stack decision 71).
    comparisons: dict[tuple[str, str], Comparison] = field(default_factory=dict)
    compare_error: Exception | None = None
    compare_calls: list[tuple[str, str]] = field(default_factory=list)

    def snapshot(self, repo: str, issue_number: int) -> StateSource:
        if repo != self.repo:
            raise LookupError(f"no fake state for {repo!r}")
        assert issue_number == self.issue.number
        return _CountedIssue(
            self,
            SnapshotState(
                repo=self.repo,
                issue_state=self.issue,
                branch_states=tuple(self.branches),
                pull_request_states=tuple(self.pulls),
                issue_comment_states=tuple(self.issue_comments),
                pr_comment_states_by_number={
                    number: tuple(comments) for number, comments in self.pr_comments.items()
                },
                review_states_by_number={
                    number: tuple(reviews) for number, reviews in self.pr_reviews.items()
                },
                review_verdicts_by_number=dict(self.verdicts),
                permissions_by_login=dict(self.permissions),
                check_runs_by_sha=dict(self.check_runs_by_sha),
            ),
        )

    async def labelled_issues(self, repo: str, label: str) -> tuple[IssueState, ...] | None:
        """The parked listing (decision 30): this one issue, if it carries `label`."""
        self.listing_calls.append((repo, label))
        if self.listing_unavailable or repo != self.repo:
            return None if self.listing_unavailable else ()
        issue = self.listing_issue or self.issue
        return (issue,) if label in issue.labels else ()

    def mark_ci_green(self, sha: str, *, name: str = "ci") -> None:
        self.check_runs_by_sha[sha] = (
            *self.check_runs_by_sha.get(sha, ()),
            CheckRunState(name=name, status="completed", conclusion="success"),
        )

    async def tree(self, repo: str, sha: str) -> tuple[TreeEntry, ...]:
        self.tree_calls.append(sha)
        if self.tree_error is not None:
            raise self.tree_error
        return self.trees.get(sha, ())

    async def compare(self, repo: str, base: str, head: str) -> Comparison:
        self.compare_calls.append((base, head))
        if self.compare_error is not None:
            raise self.compare_error
        return self.comparisons.get((base, head), Comparison("identical", 0, (), ()))

    def push_commit(
        self,
        branch: str,
        commit: str,
        files: Mapping[str, str | None],
        *,
        parent: str,
        sizes: Mapping[str, int] | None = None,
    ) -> None:
        """`branch` moves to `commit`, whose tree is `parent`'s with `files` applied (a None blob
        removes the path); comparing `parent` with `commit` lists exactly those files."""
        tree = {entry.path: entry for entry in self.trees.get(parent, ())}
        changed: list[ComparedFile] = []
        for path, blob in files.items():
            if blob is None:
                tree.pop(path, None)
                changed.append(ComparedFile(path, "removed", 0, 1))
            else:
                status = "modified" if path in tree else "added"
                tree[path] = TreeEntry(path=path, sha=blob, size=(sizes or {}).get(path, 100))
                changed.append(ComparedFile(path, status, 1, 0, blob=blob))
        self.trees[commit] = tuple(tree.values())
        self.comparisons[(parent, commit)] = Comparison("ahead", 1, (), tuple(changed))
        self.branches = [b for b in self.branches if b.name != branch]
        self.branches.append(BranchState(name=branch, sha=commit))

    def push_page(
        self, branch: str, path: str, *, commit: str, blob: str, size: int = 2048
    ) -> None:
        """An agent publishes a page: `branch` now points at `commit`, whose tree is the page."""
        self.branches = [b for b in self.branches if b.name != branch]
        self.branches.append(BranchState(name=branch, sha=commit))
        self.trees[commit] = (TreeEntry(path=path, sha=blob, size=size),)

    # --- what an agent would do, and what a human would do ------------------------------------

    def deliver_spec(self, ticket: int) -> None:
        self.branches.append(BranchState(name=f"spec/{ticket}-widget", sha="sha-1"))
        self.pulls.append(
            PullRequestState(
                number=101,
                head_ref=f"spec/{ticket}-widget",
                base_ref="main",
                head_sha="sha-1",
                state="open",
                merged=False,
                url="https://github.test/pr/101",
            )
        )

    def approve(
        self,
        login: str,
        body: str = "",
        comments: tuple[CommentState, ...] = (),
        created_at: str = "2026-08-19T12:00:00Z",
        *,
        bot: bool = False,
    ) -> None:
        review_id = self._next_review_id()
        for pr in self.pulls:
            verdict = self.verdicts.get(pr.number, ReviewVerdict())
            self.verdicts[pr.number] = replace(
                verdict,
                approvals=(*verdict.approvals, login),
                review_ids={**verdict.review_ids, login: review_id},
                # Submitted on the head each PR has now, as GitHub records a review's commit.
                commits={**verdict.commits, login: pr.head_sha},
                bots=(verdict.bots | {login}) if bot else verdict.bots,
            )
        self._record_review(login, body, comments, created_at, review_id, bot=bot)

    def push(self, number: int, sha: str) -> None:
        """A new commit lands on PR `number`'s head. Reviews keep the commit they were given on,
        as they do on GitHub when the base branch does not dismiss stale approvals."""
        self.pulls = [replace(pr, head_sha=sha) if pr.number == number else pr for pr in self.pulls]

    def request_changes(
        self,
        login: str,
        body: str = "",
        comments: tuple[CommentState, ...] = (),
        created_at: str = "2026-08-19T12:00:00Z",
        *,
        bot: bool = False,
    ) -> None:
        review_id = self._next_review_id()
        for pr in self.pulls:
            verdict = self.verdicts.get(pr.number, ReviewVerdict())
            self.verdicts[pr.number] = replace(
                verdict,
                changes_requested=(*verdict.changes_requested, login),
                review_ids={**verdict.review_ids, login: review_id},
                bots=(verdict.bots | {login}) if bot else verdict.bots,
            )
        self._record_review(login, body, comments, created_at, review_id, bot=bot)

    def _next_review_id(self) -> int:
        self.review_count += 1
        return self.review_count

    def _next_comment_id(self) -> int:
        self.comment_count += 1
        return 1000 + self.comment_count

    def _record_review(
        self,
        login: str,
        body: str,
        comments: tuple[CommentState, ...],
        created_at: str = "2026-08-19T12:00:00Z",
        review_id: int = 0,
        *,
        bot: bool = False,
    ) -> None:
        """The review's own body plus its inline comments — recorded against every
        currently-open PR, mirroring `approve`/`request_changes`'s own no-PR-number simplicity
        above (tests only ever have one open PR in flight when these are called)."""
        review = ReviewState(
            author=login,
            body=body,
            created_at=created_at,
            comments=comments,
            id=review_id,
            bot=bot,
        )
        for pr in self.pulls:
            self.pr_reviews.setdefault(pr.number, []).append(review)

    def close_pr_unmerged(self) -> None:
        self.pulls = [replace(pr, state="closed", merged=False) for pr in self.pulls]

    def merge_externally(self) -> None:
        """A human merges outside the orchestrator — GitHub then reports the PR closed AND
        merged, with any approvals frozen in review history."""
        self.pulls = [replace(pr, state="closed", merged=True) for pr in self.pulls]

    def add_issue_comment(
        self, author: str, body: str, created_at: str = "2026-08-19T12:00:00Z"
    ) -> None:
        self.issue_comments.append(
            CommentState(
                author=author,
                body=body,
                created_at=created_at,
                id=self._next_comment_id(),
            )
        )

    def add_pr_comment(
        self, number: int, author: str, body: str, created_at: str = "2026-08-19T12:00:00Z"
    ) -> None:
        self.pr_comments.setdefault(number, []).append(
            CommentState(
                author=author,
                body=body,
                created_at=created_at,
                id=self._next_comment_id(),
            )
        )


@dataclass(slots=True)
class _CountedIssue:
    """`SnapshotState` with the issue read counted on `github.issue_reads`, once per instance —
    mirroring the real `GitHubStateSource`'s own `AsyncOnce` memoization (`github/state.py`), so
    a tick's own repeat reads off the same source (the top-of-tick fetch, a precondition
    re-evaluated on re-entry) cost what one real network read costs, not one per call. Not
    frozen: the count needs a place to live across those repeat calls."""

    github: FakeGitHub
    inner: SnapshotState
    _read: bool = False
    _read_repo: bool = False

    @property
    def repo(self) -> str:
        return self.inner.repo

    def _repo_read(self) -> None:
        if not self._read_repo:
            self._read_repo = True
            self.github.repo_reads += 1

    async def issue(self) -> IssueState:
        if not self._read:
            self._read = True
            self.github.issue_reads += 1
        return await self.inner.issue()

    async def branches(self) -> Sequence[BranchState]:
        self._repo_read()
        return await self.inner.branches()

    async def pull_requests(self) -> Sequence[PullRequestState]:
        self._repo_read()
        return await self.inner.pull_requests()

    async def files(self, ref: str) -> Sequence[str]:
        self._repo_read()
        return await self.inner.files(ref)

    async def check_runs(self, sha: str) -> Sequence[CheckRunState]:
        self._repo_read()
        return await self.inner.check_runs(sha)

    async def issue_comments(self) -> Sequence[CommentState]:
        self._repo_read()
        return await self.inner.issue_comments()

    async def pr_comments(self, number: int) -> Sequence[CommentState]:
        self._repo_read()
        return await self.inner.pr_comments(number)

    async def pr_reviews(self, number: int) -> Sequence[ReviewState]:
        self._repo_read()
        return await self.inner.pr_reviews(number)

    async def pr_review_verdict(self, number: int) -> ReviewVerdict:
        self._repo_read()
        return await self.inner.pr_review_verdict(number)

    async def repo_permission(self, login: str) -> RepoPermission:
        self._repo_read()
        self.github.permission_reads.append(login)
        return await self.inner.repo_permission(login)


@dataclass
class RecordingProjector:
    """Records every mutation and applies it to the fake repo, so the next tick observes it."""

    github: FakeGitHub
    calls: list[tuple[str, Any]] = field(default_factory=list)
    #: `(issue or PR number, body)` for each comment: `calls` keeps the body alone.
    comments: list[tuple[int, str]] = field(default_factory=list)
    #: `PR number -> the sha each merge_pr call was conditioned on`, in call order.
    merge_shas: dict[int, list[str | None]] = field(default_factory=dict)
    #: When set, `merge_pr` raises it instead of merging — GitHub declining the merge.
    refuse_merge: MergeRefused | None = None

    async def set_labels(self, issue_number: int, labels: Any) -> None:
        resolved = frozenset(labels)
        self.calls.append(("set_labels", resolved))
        self.github.issue = replace(self.github.issue, labels=resolved)

    async def set_assignees(self, issue_number: int, logins: Any) -> None:
        resolved = tuple(logins)
        self.calls.append(("set_assignees", resolved))
        self.github.issue = replace(self.github.issue, assignees=resolved)

    async def comment(self, issue_number: int, body: str) -> None:
        self.calls.append(("comment", body))
        self.comments.append((issue_number, body))

    async def close_issue(self, issue_number: int) -> None:
        self.calls.append(("close_issue", issue_number))
        self.github.issue = replace(self.github.issue, state="closed")

    async def merge_pr(
        self,
        pr_number: int,
        *,
        merge_method: Any = "squash",
        commit_title: str | None = None,
        sha: str | None = None,
    ) -> str:
        self.merge_shas.setdefault(pr_number, []).append(sha)
        if self.refuse_merge is not None:
            raise self.refuse_merge
        self.calls.append(("merge_pr", pr_number))
        self.github.pulls = [
            replace(pr, merged=True, state="closed") if pr.number == pr_number else pr
            for pr in self.github.pulls
        ]
        return "merge-sha"

    def kinds(self) -> list[str]:
        return [name for name, _ in self.calls]


@dataclass
class RecordingContractGitHub:
    """`actions.registry.ContractGitHub`, backed by the same `FakeGitHub` the rest of a tick
    observes — so a successful `fix_the_contract` is visible to the NEXT tick's `StateSource`
    exactly as a real mutation would be (the tests PR closes, the tests branch disappears)."""

    github: FakeGitHub
    calls: list[tuple[str, Any]] = field(default_factory=list)
    #: Lets an infrastructure-class test simulate a `RequestFailed`-shaped failure AT the tag
    #: write itself, proving `_start_action`'s permanent-classification wrap does not
    #: overreach into this region — `github.contract.GitHubContractOps`'s own `create_or_move_tag`
    #: has exactly one exception type in its real implementation, `githubkit.exception.
    #: RequestFailed`, which this fake need not import to stand in for (`_FakeRequestFailed`).
    fail_create_or_move_tag: Exception | None = None
    #: A branch delete that fails, as GitHub refusing it would: a restart's cleanup mid-way.
    fail_delete_branch: Exception | None = None

    async def tag_sha(self, tag: str) -> str | None:
        return self.github.tags.get(tag)

    async def create_or_move_tag(self, tag: str, sha: str) -> None:
        if self.fail_create_or_move_tag is not None:
            raise self.fail_create_or_move_tag
        self.calls.append(("create_or_move_tag", tag))
        self.github.tags[tag] = sha

    async def close_pull_request(self, number: int, *, comment: str) -> None:
        self.calls.append(("close_pull_request", number))
        self.github.pulls = [
            replace(pr, state="closed") if pr.number == number else pr for pr in self.github.pulls
        ]

    async def delete_branch(self, name: str) -> None:
        if self.fail_delete_branch is not None:
            raise self.fail_delete_branch
        self.calls.append(("delete_branch", name))
        self.github.branches = [b for b in self.github.branches if b.name != name]

    async def remove_label(self, pr_number: int, label: str) -> None:
        self.calls.append(("remove_label", (pr_number, label)))


class FakeAdapter:
    """A substrate that reports whatever the test tells it to — including a lie.

    One session at a time: a fresh spawn starts `session-<run id>`, and a spawn that resumes
    continues it only when `resume_from` names it, its slice of the log starting at `log_end`.
    `status()` reports IDLE while the run is RUNNING and the session idle, and INTERRUPTED after
    `interrupt` until a `resume` succeeds.
    A finish settles through `complete`, so `status()` and `result()` agree, and never overrides
    a terminal status. `result()` carries the session's id unless `complete` named one."""

    supports_callbacks: ClassVar[bool] = False
    supports_mcp: ClassVar[bool] = True
    supports_blind_workspace: ClassVar[bool] = False
    supports_attachments: ClassVar[bool] = True
    max_concurrent_runs: ClassVar[int | None] = None
    pinned_version: ClassVar[str] = "fake"

    def __init__(self) -> None:
        self.spawns: list[RunSpec] = []
        self.cancelled: list[str] = []
        self.interrupted: list[str] = []
        self.interrupt_reasons: list[ParkReason] = []
        self.resumed: list[tuple[str, str | None]] = []
        #: `(attachments, missing_attachments)` per `resume` — what reached the resumed turn.
        self.resumed_attachments: list[
            tuple[tuple[RunAttachment, ...], tuple[MissingAttachment, ...]]
        ] = []
        #: Set by a test to make the substrate refuse — a vanished workspace, an expired
        #: session. The loop must re-park rather than start a fresh run.
        self.resume_error: Exception | None = None
        #: Raised by `result()`: the run's local state is gone.
        self.result_error: Exception | None = None
        #: `status()` calls, by run id.
        self.status_calls: list[str] = []
        self.status_value: RunStatus = RunStatus.RUNNING
        self._result: dict[str, Any] = {"outcome": RunStatus.SUCCEEDED}
        self._spawn_error: Exception | None = None
        self.session = SessionState()
        #: Every `spawn` call, refused ones included; `spawns` holds only the launched.
        self.spawn_attempts: list[RunSpec] = []
        self.finish_calls: list[str] = []
        self.session_state_calls: list[str] = []
        #: False keeps a finished run IDLE, as a slow substrate would, until a test sets it back.
        self.finish_settles = True
        #: True lands a `finish` as a new turn instead: the session goes active and runs on.
        self.finish_reports_running = False
        #: Raised by a `spawn` whose spec carries `resume_from`, whatever session it names.
        self.spawn_error_for_resume: Exception | None = None
        #: Where the session log ends: a resumed attempt's slice starts there.
        self.log_end = 0
        #: What `spawn_refusal` answers, or raises: an unverified build, an E2B outage.
        self.refusal: str | None = None
        self.refusal_error: Exception | None = None
        self.refusal_asks = 0
        self._finishing: set[str] = set()

    def complete(self, outcome: RunStatus = RunStatus.SUCCEEDED, **kwargs: Any) -> None:
        self.status_value = outcome
        self._result = {"outcome": outcome, **kwargs}

    def restart(self) -> None:
        self.status_value = RunStatus.RUNNING

    def quit_by_person(self, **kwargs: Any) -> None:
        """A person ended the CLI from its terminal: the runtime parks a clean exit nobody asked
        for, its workspace kept for a resume. `kwargs` are the rest of its result."""
        self.complete(
            RunStatus.INTERRUPTED,
            park_reason=ParkReason.EXITED,
            failure_reason=(
                "ended from its terminal with nobody asking; the workspace is kept for a resume"
            ),
            **kwargs,
        )

    def fail_spawn(self, exc: Exception) -> None:
        """The substrate's `spawn` raises `exc` instead of starting a run — the
        infrastructure-class test needs this: a `spawn()` failure that is not one of the four
        PERMANENT exception classes must roll back byte for byte, exactly as before."""
        self._spawn_error = exc

    def _session_with(self, **changes: Any) -> None:
        self.session = SessionState.model_validate({**self.session.model_dump(), **changes})

    def go_idle(
        self,
        reason: IdleReason,
        *,
        since: datetime,
        question: str | None = None,
        turn_error: str | None = None,
        question_expires_at: datetime | None = None,
    ) -> None:
        self._session_with(
            idle_reason=reason,
            idle_since=since,
            question=question,
            turn_error=turn_error,
            question_expires_at=question_expires_at,
        )

    def go_active(self) -> None:
        self._session_with(
            idle_reason=None,
            idle_since=None,
            question=None,
            turn_error=None,
            question_expires_at=None,
        )

    def typed(self, at: datetime) -> None:
        self._session_with(last_input_at=at)

    def set_deadline(self, at: datetime) -> None:
        self._session_with(session_deadline=at)

    async def spawn(self, run_spec: RunSpec) -> RunHandle:
        self.spawn_attempts.append(run_spec)
        resume_from = run_spec.resume_from
        if resume_from is not None:
            if self.spawn_error_for_resume is not None:
                raise self.spawn_error_for_resume
            if resume_from.session_id != self.session.session_id:
                raise SessionUnavailable(f"no session {resume_from.session_id!r} to continue")
        if self._spawn_error is not None:
            raise self._spawn_error
        self.spawns.append(run_spec)
        handle = RunHandle(run_id=f"run-{len(self.spawns)}")
        if resume_from is None:
            self.session = SessionState(session_id=f"session-{handle.run_id}")
        else:
            self._session_with(log_offset=self.log_end)
            self.go_active()
        return handle

    async def status(self, run_id: str) -> RunStatus:
        if run_id in self._finishing and (self.finish_settles or self.status_value.is_terminal):
            self._finishing.discard(run_id)
            if self.status_value is RunStatus.RUNNING:
                self.complete(RunStatus.SUCCEEDED)
                self.go_active()
        self.status_calls.append(run_id)
        if self.status_value is RunStatus.RUNNING and self.session.idle_reason is not None:
            return RunStatus.IDLE
        return self.status_value

    async def result(self, run_id: str) -> RunResult:
        if self.result_error is not None:
            raise self.result_error
        return RunResult(run_id=run_id, **{"session_id": self.session.session_id, **self._result})

    def logs(self, run_id: str) -> AsyncIterator[str]:
        async def _lines() -> AsyncIterator[str]:
            yield f"{run_id}: no logs in tests"

        return _lines()

    async def interrupt(self, run_id: str, reason: ParkReason = ParkReason.USER) -> None:
        self.interrupted.append(run_id)
        self.interrupt_reasons.append(reason)
        self.complete(RunStatus.INTERRUPTED)

    async def resume(
        self,
        run_id: str,
        turn: str | None,
        secrets: Mapping[str, SecretStr] | None = None,
        attachments: tuple[RunAttachment, ...] = (),
        missing_attachments: tuple[MissingAttachment, ...] = (),
    ) -> RunHandle:
        #: `(run_id, turn)` — the turn is what a test asserts the human's steering reached.
        self.resumed.append((run_id, turn))
        self.resumed_secrets = dict(secrets or {})
        self.resumed_attachments.append((attachments, missing_attachments))
        if self.resume_error is not None:
            raise self.resume_error
        self.restart()
        return RunHandle(run_id=run_id, status=RunStatus.RUNNING)

    async def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)

    async def finish(self, run_id: str) -> None:
        self.finish_calls.append(run_id)
        if self.finish_reports_running:
            self.go_active()
            return
        self._finishing.add(run_id)

    async def session_state(self, run_id: str) -> SessionState:
        self.session_state_calls.append(run_id)
        launched = [f"run-{n}" for n in range(1, len(self.spawns) + 1)]
        if run_id not in launched:
            raise UnknownRunError(run_id)
        # The contract defines it only while RUNNING or IDLE, and the fake holds one session:
        # asking about an ended or superseded run is a worker bug, so fail the test loudly.
        if run_id != launched[-1] or self.status_value.is_terminal:
            raise AssertionError(f"session_state({run_id!r}) on a run that is not live")
        return self.session

    async def terminal_target(self, run_id: str) -> TerminalTarget:
        return TerminalTarget(substrate="fake", socket="/fake/tmux.sock", session=run_id)

    async def spawn_refusal(self) -> str | None:
        self.refusal_asks += 1
        if self.refusal_error is not None:
            raise self.refusal_error
        return self.refusal


class _NoMcpAdapter(FakeAdapter):
    """`supports_mcp = False`. No shipped adapter has this shape, but the contract lets one, and
    the module's own `FakeAdapter` defaults `supports_mcp` True (matching `claude_cli`), so the
    MCP capability pre-check's negative-control test needs this sibling."""

    supports_mcp: ClassVar[bool] = False


SERVICES: Final = {"services": [{"name": "web", "command": "npm start", "port": 3000}]}


def previewed_process(**preview: Any) -> ProcessConfig:
    """`_process("proc")` with a preview on `plan`, reviewed at `spec_gate`."""
    raw = _process("proc").model_dump(mode="json", by_alias=True)
    raw["steps"][0]["preview"] = {"reviewGate": "spec_gate", **preview}
    return ProcessConfig.model_validate(raw)


class PreviewAdapter(FakeAdapter):
    """A `PreviewHost` whose preview does what the test says."""

    def __init__(self) -> None:
        super().__init__()
        #: `(run_id, spec, rendered env, token)` per `preview_start`.
        self.preview_starts: list[tuple[str, PreviewSpec, dict[str, str], str]] = []
        self.preview_stops: list[str] = []
        self.preview = PreviewState(phase=PreviewPhase.STOPPED)
        self.preview_error: Exception | None = None
        #: Raised by `preview_status`, as a transient E2B failure would be.
        self.preview_status_error: Exception | None = None
        self.keep_untils: list[datetime | None] = []

    async def preview_start(
        self, run_id: str, spec: PreviewSpec, env: Mapping[str, SecretStr], *, token: str
    ) -> PreviewState:
        if self.preview_error is not None:
            raise self.preview_error
        rendered = {key: value.get_secret_value() for key, value in env.items()}
        self.preview_starts.append((run_id, spec, rendered, token))
        self.preview = PreviewState(phase=PreviewPhase.STARTING, token=token)
        return self.preview

    async def preview_status(
        self, run_id: str, *, keep_until: datetime | None = None
    ) -> PreviewState:
        self.keep_untils.append(keep_until)
        if self.preview_status_error is not None:
            raise self.preview_status_error
        return self.preview

    async def preview_stop(self, run_id: str) -> None:
        self.preview_stops.append(run_id)
        self.preview = PreviewState(phase=PreviewPhase.STOPPED, token=self.preview.token)


class InspectAdapter(PreviewAdapter):
    """An `InspectHost` too, as the E2B adapter is: one shell per run id, OPEN on its first start
    unless `opens_as` says otherwise, and STOPPED until then or after `inspect_stop`."""

    def __init__(self) -> None:
        super().__init__()
        self.shells: dict[str, InspectState] = {}
        #: `(run_id, token, limit)` per `inspect_start`, idempotent ones included.
        self.inspect_starts: list[tuple[str, str, timedelta]] = []
        self.inspect_credentials: list[Mapping[str, SecretStr]] = []
        self.inspect_stops: list[str] = []
        self.inspect_status_calls: list[str] = []
        self.opens_as = InspectPhase.OPEN
        #: Carried by a start that does not open, as a sandbox's own words would be.
        self.detail: str | None = None
        #: Stamped on a shell this fake opens.
        self.clock: Callable[[], datetime] = Clock()
        #: Raised by `inspect_start`, as a vanished workspace would be.
        self.inspect_error: Exception | None = None

    def shell(self, run_id: str) -> InspectState:
        return self.shells.get(run_id, InspectState(phase=InspectPhase.STOPPED))

    async def inspect_start(
        self, run_id: str, *, token: str, limit: timedelta, credentials: Mapping[str, SecretStr]
    ) -> InspectState:
        self.inspect_starts.append((run_id, token, limit))
        self.inspect_credentials.append(credentials)
        if self.inspect_error is not None:
            raise self.inspect_error
        current = self.shell(run_id)
        if current.token == token and current.phase in (InspectPhase.STARTING, InspectPhase.OPEN):
            return current
        opened = self.opens_as is InspectPhase.OPEN
        self.shells[run_id] = InspectState(
            phase=self.opens_as,
            token=token,
            target=TerminalTarget(substrate="fake", socket="/fake/inspect.sock", session="inspect")
            if opened
            else None,
            started_at=self.clock() if opened else None,
            detail=None if opened else self.detail,
        )
        return self.shells[run_id]

    async def inspect_status(self, run_id: str) -> InspectState:
        self.inspect_status_calls.append(run_id)
        return self.shell(run_id)

    async def inspect_stop(self, run_id: str) -> None:
        self.inspect_stops.append(run_id)
        self.shells[run_id] = InspectState(
            phase=InspectPhase.STOPPED, token=self.shell(run_id).token
        )


class InteractivePreviewAdapter(FakeAdapter):
    """A `PreviewHost` too, as the E2B adapter is, with a preview per run. A spawn that continues a
    session leaves each run's preview as `adopted` says, as the substrate's adoption moves it."""

    def __init__(self) -> None:
        super().__init__()
        self.previews: dict[str, PreviewState] = {}
        self.adopted: dict[str, PreviewState] = {}
        #: `(run_id, token)` per `preview_start`.
        self.preview_starts: list[tuple[str, str]] = []
        #: `(run_id, keep_until)` per `preview_status`.
        self.keep_untils: list[tuple[str, datetime | None]] = []

    def serve(self, run_id: str, url: str) -> None:
        self.previews[run_id] = PreviewState(
            phase=PreviewPhase.READY,
            token=self.previews[run_id].token,
            urls=(PreviewUrl(service="web", url=url),),
        )

    async def spawn(self, run_spec: RunSpec) -> RunHandle:
        handle = await super().spawn(run_spec)
        if run_spec.resume_from is not None:
            self.previews.update(self.adopted)
        return handle

    async def preview_start(
        self, run_id: str, spec: PreviewSpec, env: Mapping[str, SecretStr], *, token: str
    ) -> PreviewState:
        self.preview_starts.append((run_id, token))
        self.previews[run_id] = PreviewState(phase=PreviewPhase.STARTING, token=token)
        return self.previews[run_id]

    async def preview_status(
        self, run_id: str, *, keep_until: datetime | None = None
    ) -> PreviewState:
        self.keep_untils.append((run_id, keep_until))
        return self.previews.get(run_id, PreviewState(phase=PreviewPhase.STOPPED))

    async def preview_stop(self, run_id: str) -> None:
        current = self.previews.get(run_id)
        token = current.token if current is not None else None
        self.previews[run_id] = PreviewState(phase=PreviewPhase.STOPPED, token=token)


# --------------------------------------------------------------------------- world fixture


@dataclass
class World:
    sessions: Any
    project_id: int
    ticket_id: int
    issue_number: int
    repo: str
    github: FakeGitHub
    projector: RecordingProjector
    contract_github: RecordingContractGitHub
    adapter: FakeAdapter
    clock: Clock
    deps: ReconcileDeps
    #: The `repo` argument each `project_github.projector(...)` call actually received — guards
    #: that the call site passes the right repo (it cannot distinguish the parameter from an
    #: identical closed-over value, but it would catch a mis-ordered or wrong argument).
    projector_repos: list[str]
    #: `(repo, permissions)` per run-token mint — what scope each run's `GH_TOKEN` was actually
    #: minted with.
    minted: list[tuple[str, Mapping[str, str]]]
    #: The `pins` argument each `deps.resolve_agent(...)` call received — the ticket's own
    #: `process_version.agent_pins`, or None for a snapshot that predates them.
    resolved_pins: list[Any]
    #: The repository each call was told to read the pins from: the version's own.
    resolved_repos: list[str | None]
    media: FakeMedia
    #: What `deps.github_for` hands every project: the fakes above, with this world's bot login.
    project_github: ProjectGitHub

    @property
    def observed_by(self) -> dict[str, Any]:
        """`admit_project`'s view of the project through its GitHub."""
        return {
            "state_source": self.project_github.state_source,
            "bot_login": self.project_github.bot_login,
        }

    def use_github(self, **changes: Any) -> None:
        """Replace parts of the project's GitHub, from the next tick on."""
        self.project_github = replace(self.project_github, **changes)
        self.deps = with_github(self.deps, self.project_github)

    def mint_with(self, mint: Mint) -> None:
        """The project's run tokens come from `mint`, from the next tick on."""
        self.use_github(client=MintingClient(mint))


#: `(repo, permissions) -> token`, as a test supplies a run-token mint.
Mint = Callable[[str, Mapping[str, str]], Awaitable[str]]


class MintingClient(GitHubClient):
    """A client with no connection whose run-token mint is the test's: every other call
    refuses, and nothing else in a tick makes one."""

    def __init__(self, mint: Mint) -> None:
        super().__init__(None)
        self._mint = mint

    async def mint_run_token(self, repo: str, permissions: Mapping[str, str]) -> RunToken:
        token = await self._mint(repo, permissions)
        return RunToken(token=SecretStr(token), expires_at=None, repo=repo)


def with_github(deps: ReconcileDeps, github: ProjectGitHub | None) -> ReconcileDeps:
    """`deps` with every project resolving to `github`; None leaves every project unconnected."""

    async def resolve(_session: AsyncSession, _project: Project) -> ProjectGitHub | None:
        return github

    return replace(deps, resolve_github=resolve)


def _scope(maker: async_sessionmaker[AsyncSession]) -> Any:
    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        session = maker()
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
        else:
            await session.commit()
        finally:
            await session.close()

    return scope


#: The deployment's terminal switch on, as ECS sets it: a browser can attach and type.
SWITCH_ON: Final = SessionPolicy(terminal_enabled=True)


async def _build_world(
    maker: async_sessionmaker[AsyncSession],
    *,
    labels: frozenset[str] = frozenset({"factorial:new"}),
    assignees: tuple[str, ...] = ("operator",),
    admit: bool = True,
    adapter_cls: type[FakeAdapter] = FakeAdapter,
    resolved_agent: ResolvedAgent = RESOLVED_AGENT,
    config: ProcessConfig | None = None,
    owner_at_admission: str = "operator",
    bot_login: str = "",
    runtimes: Mapping[str, tuple[str, FakeAdapter]] | None = None,
    agent_pins: dict[str, Any] | None = None,
    sessions: SessionPolicy = SWITCH_ON,
) -> World:
    tag = uuid4().hex[:12]
    repo = f"acme/{tag}"
    config = config if config is not None else _process(f"proc-{tag}")
    issue_number = 12
    adapter = adapter_cls()
    if config.runtime is None:
        # Every process names a runtime: one the test did not choose is a host claude row that
        # `world.adapter` serves.
        slug = _runtime_slug()
        config = ProcessConfig.model_validate(
            config.model_dump(mode="json", by_alias=True) | {"runtime": slug}
        )
        runtimes = {**(runtimes or {}), slug: ("claude_api", adapter)}

    scope = _scope(maker)
    async with scope() as session:
        # The world's one lane is its default, so an issue needs no `factorial:process:` label —
        # what a project migrated onto multiple processes gets (revision 0014's seed).
        organization_id = await default_organization_id(session)
        project = Project(
            organization_id=organization_id,
            name=f"proj-{tag}",
            github_repo=repo,
            settings={DEFAULT_KEY: config.name},
        )
        session.add(project)
        await session.flush()
        source = TicketSource(
            project_id=project.id, kind=TicketSourceKind.GITHUB, external_ref=repo
        )
        version = ProcessVersion(
            project_id=project.id,
            source_repo=project.github_repo,
            process_key=config.name,
            content=config.model_dump(mode="json", by_alias=True),
            source_commit_sha=f"{tag}{'0' * (40 - len(tag))}",
            agent_pins=agent_pins,
        )
        session.add_all([source, version])
        # Registry rows a process may name by `runtime: <slug>` — `slug -> (type, adapter)`.
        # The row is real (the worker loads it by slug, later by id); only its adapter is fake.
        session.add_all(
            Runtime(
                organization_id=organization_id,
                slug=slug,
                name=slug,
                type=rt,
            )
            for slug, (rt, _) in (runtimes or {}).items()
        )
        await session.flush()
        ticket_id = 0
        if admit:
            ticket = Ticket(
                project_id=project.id,
                ticket_source_id=source.id,
                external_key=str(issue_number),
                process_version_id=version.id,
                owner_at_admission=owner_at_admission,
                current_step=config.entry.step,
                status=TicketStatus.RUNNING,
            )
            session.add(ticket)
            await session.flush()
            ticket_id = ticket.id
        project_id = project.id
        source_id = source.id

    github = FakeGitHub(
        repo=repo,
        issue=IssueState(
            number=issue_number,
            title="Add a widget",
            body="please add a widget",
            state="open",
            labels=labels,
            assignees=assignees,
        ),
    )
    projector = RecordingProjector(github=github)
    contract_github = RecordingContractGitHub(github=github)
    media = FakeMedia()
    clock = Clock()

    minted: list[tuple[str, Mapping[str, str]]] = []

    async def mint(repo_arg: str, permissions: Mapping[str, str]) -> str:
        # Both arguments recorded: the scope a run is minted with is a security fact, so a test
        # can assert it the same way it asserts which repo was scoped (design.md §7.1 item 5).
        minted.append((repo_arg, dict(permissions)))
        return "ghs_faketoken"

    resolved_pins: list[Any] = []
    resolved_repos: list[str | None] = []

    async def resolve(
        _session: AsyncSession,
        _project: Project,
        _agent: str,
        pins: Any = None,
        repo: str | None = None,
    ) -> ResolvedAgent:
        # Recorded, not used: what the tick handed through is the fact under test — a ticket's
        # agents must resolve at its own snapshot's pins, from its own snapshot's repository, not
        # the shared registry row's or the project's.
        resolved_pins.append(pins)
        resolved_repos.append(repo)
        return resolved_agent

    projector_repos: list[str] = []

    def choose_projector(repo_arg: str) -> Any:
        # `repo_arg` is what the call site actually passed (`tick.project.github_repo`), so a
        # test can prove the factory's own parameter is what flows through.
        projector_repos.append(repo_arg)
        return projector

    null_client = GitHubClient(None)
    project_github = ProjectGitHub(
        client=MintingClient(mint),
        state_source=github.snapshot,
        projector=choose_projector,
        contents=RepoContents(null_client),
        media=lambda _repo: media,
        trees=github.tree,
        compare=github.compare,
        contract_github=lambda _repo: contract_github,
        bot_login=bot_login,
    )
    deps = ReconcileDeps(
        now=clock,
        resolve_agent=resolve,
        # The real cache over fakes: a build is a lookup by slug, so the instance-reuse and
        # by-id semantics under test are production's own. The loader is the real one — the
        # rows are real.
        runtime_adapters=RuntimeAdapters(
            build=lambda row, kind: {s: a for s, (_, a) in (runtimes or {}).items()}[row.slug]
        ),
        load_runtime=load_runtime_by_slug,
        sessions=sessions,
    )
    deps = with_github(deps, project_github)
    world = World(
        sessions=scope,
        project_id=project_id,
        ticket_id=ticket_id,
        issue_number=issue_number,
        repo=repo,
        github=github,
        projector=projector,
        contract_github=contract_github,
        adapter=adapter,
        clock=clock,
        deps=deps,
        projector_repos=projector_repos,
        minted=minted,
        resolved_pins=resolved_pins,
        resolved_repos=resolved_repos,
        media=media,
        project_github=project_github,
    )
    world.source_id = source_id  # type: ignore[attr-defined]
    return world


async def world_runtime_id(world: World) -> int:
    """The id of the Runtime row the world's process names, for a run a test writes itself."""
    async with world.sessions() as session:
        content = (
            await session.execute(
                select(ProcessVersion.content)
                .join(Ticket, Ticket.process_version_id == ProcessVersion.id)
                .where(Ticket.id == world.ticket_id)
            )
        ).scalar_one()
        return (
            await session.execute(select(Runtime.id).where(Runtime.slug == content["runtime"]))
        ).scalar_one()


async def interactive_world(
    maker: async_sessionmaker[AsyncSession],
    *,
    preview: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
    host: type[FakeAdapter] = FakeAdapter,
    **kw: Any,
) -> tuple[World, FakeAdapter]:
    """A world whose process runs on a registry runtime served by a `host` that is also
    `world.adapter`, with the deployment's switch on unless `sessions` says otherwise.
    `preview` becomes `plan`'s `preview:` block, and `plan`'s keys replace the step's own."""
    slug = _runtime_slug()
    adapter = host()
    config = _process_on_runtime(f"proc-{slug}", slug)
    if preview is not None or plan is not None:
        raw = config.model_dump(mode="json", by_alias=True)
        if preview is not None:
            raw["steps"][0]["preview"] = preview
        raw["steps"][0].update(plan or {})
        config = ProcessConfig.model_validate(raw)
    world = await _build_world(
        maker,
        config=config,
        runtimes={slug: ("claude_api", adapter)},
        **kw,
    )
    world.adapter = adapter
    return world, adapter


# --------------------------------------------------------------------------- helpers

#: `reconcile_ticket` now renews the ticket's lease (as this exact owner/ttl) immediately before
#: every agent spawn (decision 41) — every direct call in this module below needs a real, held lease
#: for that renewal to succeed, the same way a live tick already has one from `loop.tick()`'s own
#: `acquire_leases`. A test that means to exercise lease LOSS grants no lease, or a mismatched
#: one, and calls `reconcile_ticket` directly instead of going through `run_tick`.
_LEASE_OWNER: Final = "test-worker"
_LEASE_TTL: Final = timedelta(seconds=300)


async def _grant_lease(
    session: AsyncSession, ticket: Ticket, *, owner: str, ttl: timedelta
) -> None:
    """Force THIS ONE ticket's lease to `owner`/`ttl`, whatever it currently holds — scoped by id,
    never a table scan: `leases.acquire_leases`'s `ORDER BY id LIMIT` would just as happily hand
    back an unrelated ticket left over from an earlier test in this same (session-scoped, never
    truncated between tests) database. Lets a test set up a specific lease scenario (about to be
    renewed; held by someone else) without touching any ticket but its own.

    The expiry is computed by Postgres's own `now()`, mirroring `leases._expires_in` exactly
    (`worker/leases.py`'s own module docstring: "`now()` is the *database* clock throughout, so
    runners with skewed clocks still agree") — never Python's `datetime.now()`, which would only
    agree with the DB clock by luck."""
    expires_at = sql_text("now() + make_interval(secs => :ttl_seconds)").bindparams(
        ttl_seconds=ttl.total_seconds()
    )
    stmt = (
        update(Ticket)
        .where(Ticket.id == ticket.id)
        .values(lease_owner=owner, lease_expires_at=expires_at)
        .returning(Ticket.id)
    )
    assert (await session.execute(stmt)).first() is not None


async def _ensure_leased(session: AsyncSession, ticket: Ticket) -> None:
    """Grant `_LEASE_OWNER` the ticket's lease if nothing already holds it. A no-op once granted;
    300s (`_LEASE_TTL`) vastly outlives any test's real wall-clock runtime, so one grant per
    ticket covers every `run_tick` call across the test."""
    if ticket.lease_owner is None:
        await _grant_lease(session, ticket, owner=_LEASE_OWNER, ttl=_LEASE_TTL)


async def run_tick(world: World, *, listings: bool = False) -> None:
    """One reconciliation, in its own transaction — the shape `loop.tick` uses. `listings`
    hands the reconcile a per-tick parked cache over the fake, as production's tick does."""
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        await _ensure_leased(session, ticket)
        await reconcile_ticket(
            session,
            ticket,
            world.deps,
            lease_owner=_LEASE_OWNER,
            lease_ttl=_LEASE_TTL,
            listings=ParkedListings(lambda _client: world.github) if listings else None,
        )


async def ticket_row(world: World) -> Ticket:
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        return ticket


async def step_runs(world: World) -> Sequence[StepRun]:
    async with world.sessions() as session:
        return list(
            (
                await session.execute(
                    select(StepRun).where(StepRun.ticket_id == world.ticket_id).order_by(StepRun.id)
                )
            )
            .scalars()
            .all()
        )


async def plan_runs(world: World) -> list[StepRun]:
    return [run for run in await step_runs(world) if run.step_name == "plan"]


async def request_changes(world: World, feedback: str = "Make the button blue.") -> int:
    """What `POST /step-runs/{id}/changes` records, written directly: this is the loop's half."""
    runs = await step_runs(world)
    plan = [run for run in runs if run.step_name == "plan"][-1]
    gate = [run for run in runs if run.step_name == "spec_gate"][-1]
    async with world.sessions() as session:
        event = await append(
            session,
            type=EventType.STEP_CHANGES_REQUESTED,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            step_run_id=gate.id,
            step=gate.step_name,
            attempt=gate.attempt,
            reviewed_step="plan",
            reviewed_step_run_id=plan.id,
            reviewed_attempt=plan.attempt,
            feedback=feedback,
            requested_by="reviewer@example.com",
            idempotency_key=uuid4().hex,
        )
        return event.id


def change_request_inputs(spec: RunSpec) -> list[str]:
    return [item.value for item in spec.inputs if item.name == "change_request"]


async def event_rows(world: World) -> Sequence[Event]:
    async with world.sessions() as session:
        return list(
            (
                await session.execute(
                    select(Event).where(Event.ticket_id == world.ticket_id).order_by(Event.id)
                )
            )
            .scalars()
            .all()
        )


async def event_types(world: World) -> list[str]:
    return [row.type for row in await event_rows(world)]


async def entered_events(world: World) -> list[Mapping[str, Any]]:
    """Every `step_entered` detail blob, in log order — one per `current_step` assignment."""
    return [row.details for row in await event_rows(world) if row.type == "step_entered"]


async def store_secret(world: World, name: str, value: str) -> None:
    """A project secret, as an operator stores it (`secrets.store.put`)."""
    async with world.sessions() as session:
        await put(session, world.project_id, name, value)


async def advance_to_gate(world: World, **result: Any) -> None:
    """plan spawns → plan succeeds and is accepted → the ticket sits on `spec_gate`."""
    await run_tick(world)  # spawn
    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(RunStatus.SUCCEEDED, **result)
    await run_tick(world)  # accept + advance
    await run_tick(world)  # open the gate


async def _escalate_the_plan_step(world: World) -> None:
    """Drive `plan` through both configured attempts to escalation — the identical sequence
    `test_retry_exhaustion_escalates` uses, factored out because every un-block test below starts
    from the same BLOCKED ticket."""
    await run_tick(world)  # attempt 1 spawns
    world.adapter.complete(RunStatus.FAILED, failure_reason="agent gave up")
    await run_tick(world)  # attempt 1 fails -> retry scheduled

    world.clock.advance(timedelta(minutes=11))
    world.adapter.restart()
    await run_tick(world)  # attempt 2 spawns
    world.adapter.complete(RunStatus.FAILED, failure_reason="agent gave up again")
    await run_tick(world)  # attempt 2 fails -> escalate, BLOCKED


def _remove_blocked_label(world: World) -> None:
    """The human's edit `_grant_attempt` reacts to: the blocked label gone, everything else
    untouched. Mutates `world.github.issue` directly (never through `world.projector`) — this is an
    OBSERVED change the orchestrator did not make, exactly like a human editing GitHub.

    Every spelling, because a human removes the label they can SEE: an issue labelled before
    issue #138 still says `adlc:blocked` until the next projection rewrites it."""
    world.github.issue = replace(
        world.github.issue, labels=world.github.issue.labels - BLOCKED_LABELS
    )


def add_label(world: World, label: str) -> None:
    """A human's edit on GitHub — never through `world.projector`, which is the loop's pen."""
    world.github.issue = replace(world.github.issue, labels=world.github.issue.labels | {label})


def remove_label(world: World, label: str) -> None:
    world.github.issue = replace(world.github.issue, labels=world.github.issue.labels - {label})


def close_issue(world: World) -> None:
    world.github.issue = replace(world.github.issue, state="closed")


async def request(world: World, event_type: EventType) -> None:
    """What the API routes record. Written directly so this module tests the LOOP's half."""
    async with world.sessions() as session:
        await append(
            session,
            type=event_type,
            project_id=world.project_id,
            ticket_id=world.ticket_id,
            requested_via="dashboard",
        )
        await session.commit()


async def retry_waiting(world: World) -> StepRun:
    """The world's first attempt failed and its retry waits out the backoff, counted from now:
    the retry service reads the wall clock, the world's own clock is in the past."""
    await run_tick(world)
    world.adapter.complete(RunStatus.FAILED, failure_reason="agent gave up")
    await run_tick(world)
    (failed,) = await step_runs(world)
    async with world.sessions() as session:
        await session.execute(
            update(StepRun).where(StepRun.id == failed.id).values(ended_at=datetime.now(UTC))
        )
    return failed


async def running_run(world: World) -> StepRun:
    await run_tick(world)
    (run,) = await step_runs(world)
    assert run.status == StepRunStatus.RUNNING
    return run


async def exited_via_label(world: World) -> None:
    await running_run(world)
    add_label(world, LABEL_EXIT)
    await run_tick(world)
    assert (await ticket_row(world)).status == TicketStatus.EXITED


@dataclass
class _RaisingStateSource:
    """Delegates every read to the tick's real snapshot, except the named ones, which raise —
    the incident's exact shape: ONE endpoint (check-runs) answering 403 while every other read
    keeps working."""

    inner: StateSource
    failing: frozenset[str]

    def _trip(self, name: str) -> None:
        if name in self.failing:
            raise _FakeRequestFailed(f"{name}: {_THE_403}")

    @property
    def repo(self) -> str:
        return self.inner.repo

    async def issue(self) -> IssueState:
        self._trip("issue")
        return await self.inner.issue()

    async def branches(self) -> Sequence[BranchState]:
        self._trip("branches")
        return await self.inner.branches()

    async def pull_requests(self) -> Sequence[PullRequestState]:
        self._trip("pull_requests")
        return await self.inner.pull_requests()

    async def files(self, ref: str) -> Sequence[str]:
        self._trip("files")
        return await self.inner.files(ref)

    async def check_runs(self, sha: str) -> Sequence[CheckRunState]:
        self._trip("check_runs")
        return await self.inner.check_runs(sha)

    async def issue_comments(self) -> Sequence[CommentState]:
        self._trip("issue_comments")
        return await self.inner.issue_comments()

    async def pr_comments(self, number: int) -> Sequence[CommentState]:
        self._trip("pr_comments")
        return await self.inner.pr_comments(number)

    async def pr_reviews(self, number: int) -> Sequence[ReviewState]:
        self._trip("pr_reviews")
        return await self.inner.pr_reviews(number)

    async def pr_review_verdict(self, number: int) -> ReviewVerdict:
        self._trip("pr_review_verdict")
        return await self.inner.pr_review_verdict(number)

    async def repo_permission(self, login: str) -> RepoPermission:
        self._trip("repo_permission")
        return await self.inner.repo_permission(login)


def _fail_observation(world: World, *methods: str) -> None:
    """From the next snapshot on, the named `StateSource` reads raise the 403 shape."""
    failing = frozenset(methods)

    def snapshot(repo: str, issue_number: int) -> StateSource:
        return _RaisingStateSource(world.github.snapshot(repo, issue_number), failing)

    world.use_github(state_source=snapshot)


_THE_403: Final = "403 Forbidden: Resource not accessible by integration"
