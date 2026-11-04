"""Warnings for configs that parse green and then misbehave.

The parser owns errors; this module owns the hazards that validate clean and cost a run later —
a config with no retry anywhere is final on first failure, an unknown placeholder stays literal
in a branch name, a delivery predicate under `any:` derives no expected artifact. Everything
here is advisory: a warned config is still a valid config.
"""

from __future__ import annotations

from collections.abc import Iterator

from pydantic import BaseModel

from orchestrator.config.conditions import (
    DELIVERY_PREDICATE_NAMES,
    AllOf,
    AnyOf,
    Condition,
    Not,
    PredicateCall,
    iter_calls,
    iter_conjunctive,
)
from orchestrator.config.models import (
    AgentStep,
    CheckStep,
    FileInput,
    GateStep,
    Outcome,
    ProcessConfig,
)
from orchestrator.config.templates import KNOWN_PLACEHOLDERS, ROLE_PREFIX, placeholders

__all__ = ["LintWarning", "condition_sites", "lint_process"]

#: Predicates whose first argument is a branch head glob.
_HEAD_ARG_PREDICATES = frozenset(
    {"branch_exists", "pr_open", "pr_approved", "pr_changes_requested", "ci_green"}
)


class LintWarning(BaseModel):
    code: str
    loc: str
    msg: str


def _negated_calls(condition: Condition | None) -> Iterator[PredicateCall]:
    match condition:
        case AllOf():
            for child in condition.all_of:
                yield from _negated_calls(child)
        case AnyOf():
            for child in condition.any_of:
                yield from _negated_calls(child)
        case Not():
            yield from iter_calls(condition.not_)
        case _:
            return


def _outcome_strings(loc: str, outcome: Outcome | None) -> Iterator[tuple[str, str]]:
    if outcome is None:
        return
    if outcome.ticket_label:
        yield f"{loc}.ticketLabel", outcome.ticket_label
    if outcome.comment:
        yield f"{loc}.comment", outcome.comment
    for principal in outcome.assign or ():
        yield f"{loc}.assign", principal


def _call_args(loc: str, condition: Condition | None) -> Iterator[tuple[str, str]]:
    for call in iter_calls(condition):
        for arg in call.args:
            yield loc, arg


def _template_strings(config: ProcessConfig) -> Iterator[tuple[str, str]]:
    """Every config string that goes through template expansion, with a human-readable loc."""
    yield from _call_args("entry.when", config.entry.when)
    if config.defaults.on_failure is not None:
        yield from _outcome_strings(
            "defaults.onFailure.escalate", config.defaults.on_failure.escalate
        )
    for role, members in config.roles.items():
        for member in members:
            yield f"roles.{role}", member
    yield from _outcome_strings("terminal.actions", config.terminal.actions)
    for step in config.steps:
        loc = f"steps[{step.name}]"
        if isinstance(step, AgentStep):
            yield f"{loc}.workspace.base", step.workspace.base
            yield f"{loc}.workspace.branch", step.workspace.branch
            for where, condition in (
                ("preconditions", step.preconditions),
                ("postconditions", step.postconditions),
            ):
                yield from _call_args(f"{loc}.{where}", condition)
            for ref in step.inputs:
                if isinstance(ref, FileInput):
                    yield f"{loc}.inputs", ref.file
        if isinstance(step, GateStep):
            yield f"{loc}.gate.pr.head", step.gate.pr.head
            yield f"{loc}.gate.pr.base", step.gate.pr.base
            for approver in step.gate.approvers:
                yield f"{loc}.gate.approvers", approver
            yield from _outcome_strings(f"{loc}.gate.waiting", step.gate.waiting)
            yield from _outcome_strings(f"{loc}.gate.onApprove", step.gate.on_approve)
            yield from _outcome_strings(f"{loc}.gate.onReject", step.gate.on_reject)
        if isinstance(step, CheckStep):
            yield from _call_args(f"{loc}.conditions", step.conditions)
            if step.pr is not None:
                yield f"{loc}.pr.head", step.pr.head
                yield f"{loc}.pr.base", step.pr.base
        on_start = getattr(step, "on_start", None)
        yield from _outcome_strings(f"{loc}.onStart", on_start)
        on_success = getattr(step, "on_success", None)
        yield from _outcome_strings(f"{loc}.onSuccess", on_success)
        on_failure = getattr(step, "on_failure", None)
        if on_failure is not None:
            yield from _outcome_strings(f"{loc}.onFailure.escalate", on_failure.escalate)


def _static_prefix(value: str) -> str:
    return value.split("{", 1)[0].split("*", 1)[0]


def _one_typo_apart(left: str, right: str) -> bool:
    """Equal length with one substitution or one adjacent transposition — a typo, not a
    different name. Keeps the divergence check quiet for steps that legitimately reference
    another step's branch."""
    if left == right or len(left) != len(right):
        return False
    diffs = [i for i, (a, b) in enumerate(zip(left, right, strict=True)) if a != b]
    if len(diffs) == 1:
        return True
    if len(diffs) == 2 and diffs[1] == diffs[0] + 1:
        i, j = diffs
        return left[i] == right[j] and left[j] == right[i]
    return False


def condition_sites(config: ProcessConfig) -> Iterator[tuple[str, Condition]]:
    """Every condition a process evaluates, with where it sits."""
    yield "entry.when", config.entry.when
    for step in config.steps:
        if isinstance(step, AgentStep):
            if step.preconditions is not None:
                yield f"steps[{step.name}].preconditions", step.preconditions
            if step.postconditions is not None:
                yield f"steps[{step.name}].postconditions", step.postconditions
        if isinstance(step, CheckStep):
            yield f"steps[{step.name}].conditions", step.conditions


def lint_process(config: ProcessConfig) -> list[LintWarning]:
    warnings: list[LintWarning] = []

    # A step with no retry ANYWHERE is final on its first failure — the config parses, and the
    # ticket just stops. Same for escalation: the failure happens and nobody is told.
    runnable = [s for s in config.steps if not isinstance(s, GateStep)]
    if config.defaults.retry is None:
        unretried = [s.name for s in runnable if s.retry is None]
        if unretried:
            warnings.append(
                LintWarning(
                    code="missing-retry",
                    loc="defaults.retry",
                    msg=(
                        f"Step(s) {', '.join(unretried)} have no retry and defaults.retry is "
                        "unset, so a failed attempt is final. Add defaults.retry (the sample uses "
                        "maxAttempts: 2, backoff: 10m) or a per-step retry."
                    ),
                )
            )
    if config.defaults.on_failure is None:
        unescalated = [s.name for s in runnable if s.on_failure is None]
        if unescalated:
            warnings.append(
                LintWarning(
                    code="missing-escalation",
                    loc="defaults.onFailure",
                    msg=(
                        f"Step(s) {', '.join(unescalated)} have no onFailure and "
                        "defaults.onFailure is unset, so a final failure adds no label, comment "
                        "or assignee. Add defaults.onFailure.escalate."
                    ),
                )
            )

    # Placeholders: expansion leaves unknown names literal, so {tickett} reaches GitHub as-is.
    seen: set[tuple[str, str]] = set()
    for loc, text in _template_strings(config):
        for name in placeholders(text):
            if name.startswith(ROLE_PREFIX):
                role = name[len(ROLE_PREFIX) :]
                if role not in config.roles and (name, loc) not in seen:
                    seen.add((name, loc))
                    warnings.append(
                        LintWarning(
                            code="unknown-role",
                            loc=loc,
                            msg=(
                                f"{{roles.{role}}} names no role in roles:. Defined roles: "
                                f"{', '.join(sorted(config.roles)) or 'none'}."
                            ),
                        )
                    )
            elif name not in KNOWN_PLACEHOLDERS and (name, loc) not in seen:
                seen.add((name, loc))
                warnings.append(
                    LintWarning(
                        code="unknown-placeholder",
                        loc=loc,
                        msg=(
                            f"{{{name}}} is not a placeholder Factorial supplies, so it "
                            "stays literal text. Known placeholders: "
                            f"{', '.join(sorted(KNOWN_PLACEHOLDERS))}."
                        ),
                    )
                )

    # {slug} is agent-chosen per step run; before entry there is no agent, and a base branch
    # must exist before the run that would have named it.
    for call in iter_calls(config.entry.when):
        if any("{slug}" in arg for arg in call.args):
            warnings.append(
                LintWarning(
                    code="slug-misplaced",
                    loc="entry.when",
                    msg="{slug} cannot resolve at admission, because no agent has chosen one yet.",
                )
            )
    for step in config.steps:
        if isinstance(step, AgentStep) and "{slug}" in step.workspace.base:
            warnings.append(
                LintWarning(
                    code="slug-misplaced",
                    loc=f"steps[{step.name}].workspace.base",
                    msg=(
                        "{slug} in a base branch cannot resolve, because the base must exist "
                        "before the run that would have chosen the slug. Use a * glob upstream "
                        "and a concrete base here."
                    ),
                )
            )

    # ticket_label matches exactly — a glob is looked for literally and never matches.
    for loc, condition in condition_sites(config):
        for call in iter_calls(condition):
            if call.name == "ticket_label" and any(c in call.args[0] for c in "*?"):
                warnings.append(
                    LintWarning(
                        code="globbed-ticket-label",
                        loc=loc,
                        msg=(
                            f"ticket_label({call.args[0]!r}) matches the label name exactly, "
                            "not as a glob, so this condition can never hold."
                        ),
                    )
                )

    # A gate that reruns INTO a check step livelocks: the rework attempt re-evaluates the same
    # observed state and usually completes on its own entry tick, the gate reopens, and the
    # standing changes-requested review re-rejects on the same tick — nothing between the two
    # can ever change. Agent rework earns the loop by doing new work; a check cannot.
    by_name = {step.name: step for step in config.steps}
    for step in config.steps:
        if isinstance(step, GateStep) and step.gate.on_reject is not None:
            target = by_name.get(step.gate.on_reject.rerun_step)
            if isinstance(target, CheckStep):
                warnings.append(
                    LintWarning(
                        code="check-rerun",
                        loc=f"steps[{step.name}].gate.onReject.rerunStep",
                        msg=(
                            f"rerunStep {target.name!r} is a check step. Rework re-evaluates "
                            "the same observed state and completes immediately, so the gate "
                            "re-rejects in a permanent automatic loop. Point rerunStep at the "
                            "agent step whose work the review is about."
                        ),
                    )
                )

    # `{pr_url}` in a check outcome resolves only through the step's own `pr:` matcher — with
    # no `pr:` the placeholder stays literal in the projected comment.
    for step in config.steps:
        if isinstance(step, CheckStep) and step.pr is None:
            for site, text in _outcome_strings(f"steps[{step.name}].onSuccess", step.on_success):
                if "{pr_url}" in text:
                    warnings.append(
                        LintWarning(
                            code="pr-url-without-pr",
                            loc=site,
                            msg=(
                                "{pr_url} in a check step's outcome needs the step's `pr:` "
                                "matcher to resolve; without one it stays literal text."
                            ),
                        )
                    )

    for step in config.steps:
        if not isinstance(step, AgentStep):
            continue
        loc = f"steps[{step.name}]"
        own_glob = step.workspace.branch.replace("{slug}", "*")

        # Parsed for the snapshots pinned with it, and read by nothing since the summary became
        # a declared file (decision 71): a config that still says it shows no summary at all.
        if step.preview is not None and step.preview.page is not None:
            warnings.append(
                LintWarning(
                    code="retired-page",
                    loc=f"{loc}.preview.page",
                    msg=(
                        "preview.page is retired, and its page is no longer shown. List the "
                        "summary under preview.files instead, at a path on the step's own branch "
                        '(the sample uses "specs/{ticket}-{slug}/summary.html"), and say in the '
                        "agent's instructions to write it."
                    ),
                )
            )

        # `not: branch_exists(<own branch>)` as a precondition wedges rework: the first attempt
        # creates the branch, so every retry and every gate-rejected rerun is refused.
        for call in _negated_calls(step.preconditions):
            if call.name in ("branch_exists", "pr_open") and call.args[0] == own_glob:
                warnings.append(
                    LintWarning(
                        code="self-guard",
                        loc=f"{loc}.preconditions",
                        msg=(
                            f"not: {call.source} guards the step against its own branch. The "
                            "first attempt creates that branch, so rework can never run."
                        ),
                    )
                )

        # A postcondition glob one typo away from the step's own branch prefix parses green and
        # can never be delivered by this step.
        own_prefix = _static_prefix(step.workspace.branch)
        for call in iter_calls(step.postconditions):
            if call.name not in _HEAD_ARG_PREDICATES or not call.args:
                continue
            other_prefix = _static_prefix(call.args[0])
            if own_prefix and other_prefix and _one_typo_apart(own_prefix, other_prefix):
                warnings.append(
                    LintWarning(
                        code="branch-prefix-divergence",
                        loc=f"{loc}.postconditions",
                        msg=(
                            f"{call.source} is one typo away from this step's branch "
                            f"({step.workspace.branch!r}), so this step can never satisfy it."
                        ),
                    )
                )

        # Delivery predicates under any:/not: derive no expected artifact, so the substrate has
        # nothing to probe for (iter_conjunctive is the same walk the worker derives from).
        delivery = [
            c for c in iter_calls(step.postconditions) if c.name in DELIVERY_PREDICATE_NAMES
        ]
        conjunctive = [
            c for c in iter_conjunctive(step.postconditions) if c.name in DELIVERY_PREDICATE_NAMES
        ]
        if delivery and not conjunctive:
            warnings.append(
                LintWarning(
                    code="delivery-not-conjunctive",
                    loc=f"{loc}.postconditions",
                    msg=(
                        "Every delivery predicate sits under any:/not:, so no expected "
                        "artifact is derivable and the runtime cannot check for this "
                        "step's delivery. Put at least one at the top level of all:."
                    ),
                )
            )

    return warnings
