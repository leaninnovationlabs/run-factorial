"""Reading a template's substitution facts back out of a parsed process config.

A step's own `workspace.branch`/`workspace.base` settle facts a lane-wide read cannot, on a lane
whose pull requests all target the base branch.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from orchestrator.config.conditions import PredicateCall, iter_calls
from orchestrator.config.models import AgentStep, FileInput, ProcessConfig


def pr_opens(config: ProcessConfig) -> list[PredicateCall]:
    """Every two-argument `pr_open` postcondition in the lane, head first, base second."""
    return [
        call
        for step in config.steps
        if isinstance(step, AgentStep)
        for call in iter_calls(step.postconditions)
        if call.name == "pr_open" and len(call.args) == 2
    ]


def _only(values: Collection[str]) -> str | None:
    """One distinct value, or `None`. Disagreement is absence — never a majority vote."""
    distinct = {value for value in values if value}
    return next(iter(distinct)) if len(distinct) == 1 else None


def _prefix(glob: str) -> str:
    return glob.split("/")[0]


def derive_lane_facts(config: ProcessConfig) -> Mapping[str, str]:
    """The facts the prompts name, read back out of the config — only those that read cleanly.

    Ambiguity is absence, not a default: two agent steps disagreeing about `workspace.base`, or a
    lane with no stacked `pr_open`, means the fact is unavailable and any template that needs it
    is stubbed instead. A defaulted guess is how a prompt comes to contradict its own config.
    """
    steps = [step for step in config.steps if isinstance(step, AgentStep)]
    if not steps:
        return {}

    facts: dict[str, str] = {}

    # A shared component (a skill or MCP server) belongs to no single agent step, so it can only
    # ever read the lane-wide base — never a per-agent override, which is `derive_agent_facts`'s
    # job and must still win there. Disagreement across steps is absence, exactly as the other
    # lane facts behave.
    base = _only([step.workspace.base for step in steps])
    if base is not None:
        facts["__BASE_BRANCH__"] = base

    # The stacked call is the one whose BASE is itself a branch glob: `pr_open("impl/…",
    # "spec/…")`. The plan step's `pr_open("spec/…", "main")` has a bare base and identifies the
    # spec prefix on its own, which is what a non-stacked lane leaves us.
    postconditions = [call for step in steps for call in iter_calls(step.postconditions)]
    calls = pr_opens(config)
    stacked = [call for call in calls if "/" in call.args[1]]
    against_base = [call for call in calls if "/" not in call.args[1]]

    spec = _only([_prefix(call.args[1]) for call in stacked]) or _only(
        [_prefix(call.args[0]) for call in against_base]
    )
    if spec is not None:
        facts["__SPEC_PREFIX__"] = spec
    impl = _only([_prefix(call.args[0]) for call in stacked])
    if impl is not None:
        facts["__IMPL_PREFIX__"] = impl

    # `specs/{ticket}-*/spec.md` — from the file inputs a step is handed, and from any
    # `file_exists` postcondition proving one was written.
    paths = [ref.file for step in steps for ref in step.inputs if isinstance(ref, FileInput)]
    paths += [call.args[0] for call in postconditions if call.name == "file_exists" and call.args]
    specs_dir = _only([_prefix(path) for path in paths])
    if specs_dir is not None:
        facts["__SPECS_DIR__"] = specs_dir

    return facts


#: Facts that only a lane's own branch topology can settle.
_LANE_PREFIXES = frozenset({"__SPEC_PREFIX__", "__IMPL_PREFIX__"})


def stub_reason(missing: Collection[str], config: ProcessConfig) -> str:
    """Why a template was withheld, in terms of the operator's own config.

    The unstacked lane is named outright rather than reported as missing facts. "does not supply
    spec_prefix" reads as nonsense against a config whose branches are literally `spec/…`: both
    prefixes are right there, and what is actually absent is any way to tell which is which once
    every pull request targets the base branch — the shape the catalog's prompts do not describe.
    """
    calls = pr_opens(config)
    if calls and not any("/" in call.args[1] for call in calls) and _LANE_PREFIXES & set(missing):
        return (
            "Every pull request in this process targets the base branch, but the template's "
            "prompts describe a stacked process. In a stacked process the implementation pull "
            "request targets the spec branch, and the spec branch reaches the base branch only at "
            "the last gate. Those prompts would point the agent at the wrong base, so no "
            "postcondition would hold even with CI green."
        )
    return (
        "This process does not supply "
        + ", ".join(marker.strip("_").lower() for marker in sorted(missing))
        + ", which the template's prompts name. The prompts would describe a different process "
        "from the one configured."
    )


#: The substitution marker is `__UPPER_SNAKE__`, never `{}`: ADLC prose carries `{ticket}`,
#: `{slug}` and `{roles.x}` that must reach the agent verbatim (`config/templates.py` exists for
#: that reason), and `__X__` leaves an unsubstituted template file parseable as YAML, which its
#: tests rely on.
#:
#: Facts a template may ask for. A template using one the config cannot supply is not copied.
MARKERS = (
    "__AGENT_NAME__",
    "__BRANCH_PREFIX__",
    "__BASE_BRANCH__",
    "__SPEC_PREFIX__",
    "__IMPL_PREFIX__",
    "__SPECS_DIR__",
)


def is_stacked(config: ProcessConfig) -> bool:
    """Whether the implementation pull request targets a branch rather than the base branch.

    The discriminator is the BASE of a two-argument `pr_open`: a base that is itself a branch
    glob means the lane stacks. It is what decides which prompt variant describes this lane.
    """
    return any("/" in call.args[1] for call in pr_opens(config))


def _agent_step(config: ProcessConfig, name: str) -> AgentStep | None:
    """The first step running `name`. A name used twice must describe one branch shape, so the
    first is enough; two steps disagreeing is a config the templates cannot serve anyway."""
    return next(
        (step for step in config.steps if isinstance(step, AgentStep) and step.agent == name),
        None,
    )


def derive_agent_facts(config: ProcessConfig, name: str) -> Mapping[str, str]:
    """Lane facts plus the ones only this agent's own step can settle.

    A step names its own branch unambiguously, where the lane-scoped read cannot: on a lane
    whose pull requests all target the base branch, `_only()` over the `pr_open` heads sees two
    distinct prefixes and yields nothing. `workspace.branch` has no such ambiguity.
    """
    facts = {**derive_lane_facts(config), "__AGENT_NAME__": name}
    step = _agent_step(config, name)
    if step is None:
        return facts
    if step.workspace.branch:
        facts["__BRANCH_PREFIX__"] = _prefix(step.workspace.branch)
    if step.workspace.base:
        # Overrides the lane-wide value `derive_lane_facts` may have set: an agent file always
        # has one concrete step to read, where a shared component does not.
        facts["__BASE_BRANCH__"] = step.workspace.base
    return facts
