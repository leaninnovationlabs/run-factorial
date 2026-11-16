"""ADLC config models — `process.yaml` and `agent.yaml` (design.md §4).

Field names are the YAML spelling (camelCase in `process.yaml`) exposed through aliases, so
Python stays snake_case. `extra="forbid"` everywhere: a typo'd key must fail admission loudly,
because the whole parsed `ProcessConfig` is snapshotted into `process_version.content` and then
governs a ticket for its entire life. Dump that snapshot with `by_alias=True`.

Shape and graph only — conditions are evaluated by `orchestrator.predicates`, templates expanded
by `orchestrator.config.templates`.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Mapping
from datetime import timedelta
from fnmatch import fnmatchcase
from typing import Annotated, Any, Final, Literal, Self, get_args

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    PlainSerializer,
    Tag,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.alias_generators import to_camel

from orchestrator.config.conditions import (
    REVIEW_PREDICATE,
    AllOf,
    AnyOf,
    Condition,
    Not,
    awaits_review,
)
from orchestrator.config.templates import globbed, placeholders
from runtime.contract import CONTROL_PLANE_PORT

# --------------------------------------------------------------------------- spec version

#: Both names are permanent, and neither is translated on the way in. `process_version.content`
#: is append-only (design.md §2.8) and re-parsed on every tick by `worker/tick_context.py`'s
#: `_load_pinned_config`, so every snapshot written before the `.factorial` rename says
#: `adlc/v1` for ever. Narrowing this would fail the tick of every live ticket rather than
#: migrate anything. New files are written with the second (issue #138).
SpecVersion = Literal["adlc/v1", "factorial/v1"]


# --------------------------------------------------------------------------- bases


class _Base(BaseModel):
    """Snake_case keys in YAML (agent manifests)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)


class _CamelBase(BaseModel):
    """camelCase keys in YAML (`process.yaml`: ticketLabel, maxAttempts, onSuccess, ...)."""

    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, frozen=True, alias_generator=to_camel
    )


# ------------------------------------------------------------------------ durations

_DURATION = re.compile(r"^(\d+)([smh])$")
_UNIT_SECONDS: Final[Mapping[str, int]] = {"s": 1, "m": 60, "h": 3600}


def _parse_duration(value: object) -> object:
    if isinstance(value, timedelta):
        return value
    if isinstance(value, str):
        match = _DURATION.match(value.strip())
        if match is None:
            raise ValueError(
                f"Invalid duration {value!r}. Write <int><s|m|h>, for example '30s', '10m' or '1h'."
            )
        return timedelta(seconds=int(match.group(1)) * _UNIT_SECONDS[match.group(2)])
    return value


def _format_duration(value: timedelta) -> str:
    seconds = int(value.total_seconds())
    for unit, size in (("h", 3600), ("m", 60)):
        if seconds and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


#: `1h` / `10m` / `30s` in YAML, `timedelta` in code, compact string again on dump.
Duration = Annotated[
    timedelta,
    BeforeValidator(_parse_duration),
    PlainSerializer(_format_duration, return_type=str),
]


# --------------------------------------------------------------------------- inputs

BareInputSource = Literal["ticket_body", "ticket_comments", "pr_review_comments"]

#: The closed set of bare `inputs:` names; anything else must be a `{file: ...}` mapping.
BARE_INPUT_SOURCES: Final[frozenset[str]] = frozenset(get_args(BareInputSource))


class SourceInput(_Base):
    """A named input, written bare in YAML: `- ticket_body`."""

    source: BareInputSource

    @model_validator(mode="before")
    @classmethod
    def _from_scalar(cls, value: Any) -> Any:
        return {"source": value} if isinstance(value, str) else value

    @model_serializer
    def _to_scalar(self) -> str:
        return self.source


class FileInput(_CamelBase):
    """A file input: `- file: "specs/{ticket}-*/spec.md"` (globs, because of `{slug}`)."""

    file: str


def _input_tag(value: Any) -> str | None:
    if isinstance(value, str | SourceInput):
        return "source"
    if isinstance(value, FileInput):
        return "file"
    if isinstance(value, Mapping):
        if "file" in value:
            return "file"
        if "source" in value:
            return "source"
    return None


InputRef = Annotated[
    Annotated[SourceInput, Tag("source")] | Annotated[FileInput, Tag("file")],
    Discriminator(_input_tag),
]


# -------------------------------------------------------------------------- outcomes


class Outcome(_CamelBase):
    """The shared outcome block: how a transition is projected onto the ticket.

    Used by `onSuccess`, `onFailure.escalate`, `gate.waiting`, `gate.onApprove`, `gate.onReject`
    and `terminal.actions`. `assign` replaces the issue's assignees rather than adding to them
    (design.md §4); the orchestrator implements that, this model only carries it.
    """

    ticket_label: str | None = None
    comment: str | None = None
    assign: tuple[str, ...] | None = None


class Retry(_CamelBase):
    max_attempts: int = Field(ge=1)
    backoff: Duration


class OnFailure(_CamelBase):
    escalate: Outcome


class Defaults(_CamelBase):
    """Applied to agent steps (all three), action steps (`retry`/`onFailure`), and check steps
    (all three); a step may override any of them."""

    timeout: Duration | None = None
    retry: Retry | None = None
    on_failure: OnFailure | None = None


class Policy(_CamelBase):
    """What this process expects of the repository it runs in.

    Distinct from `defaults`, which configures how steps RUN. Policy describes the repository's
    shape, which the orchestrator observes and reports but does not impose.

    A NOTE ON WHERE THIS LIVES, so it is not "fixed" later. `.factorial/` sits in the repo agents
    write to, so anything here is editable by an agent — the inverse of §7.5's rule that a repo
    file selects and operator state permits. That is why `required` reports and never gates:
    enforcement an agent could switch off is worse than none, because it looks like a control.
    The real enforcement is branch protection itself plus the App holding no bypass, and neither
    is reachable from this file. Making `required` blocking would move a real control into a
    place an agent can edit; it would not add one.
    """

    #: `required` — the process expects the profile in `BranchProtection.shortfalls()`; a repo
    #: that does not meet it is reported in red. `advisory` (the default) reports a shortfall as
    #: a warning. `ignored` drops the row entirely, for a repo where protection is deliberately
    #: not used. None of the three gate activation or admission.
    #:
    #: `ignored`, NOT `off`: YAML 1.1 reads a bare `off` as the boolean false, so the most
    #: natural spelling for this value would never reach the model as a string. `_reject_bool`
    #: below says so rather than letting the reader debug a `False` they did not write.
    branch_protection: Literal["required", "advisory", "ignored"] = "advisory"

    @field_validator("branch_protection", mode="before")
    @classmethod
    def _reject_bool(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError(
                "branchProtection was read as a boolean. YAML treats a bare off/on/yes/no as "
                "true or false, so quote the value or use one of: required, advisory, ignored."
            )
        return value


# ----------------------------------------------------------------------------- steps


class Workspace(_CamelBase):
    repo: str
    base: str
    branch: str

    @model_validator(mode="after")
    def _check_repo_shape(self) -> Self:
        """Reject a literal `repo` that is not `owner/name`, at parse time.

        Otherwise it fails deep inside a spawn as a bare `ValueError` from
        `github.client.split_repo` — a type the worker's `_PERMANENT_CONFIG_ERRORS` does not name,
        so the run rolls back and retries invisibly. A templated value is left alone (what it
        expands to is unknown here) and so keeps that residual. The check mirrors `split_repo`
        exactly, to neither reject nor accept what the runtime would treat differently.
        """
        if placeholders(self.repo):
            return self
        owner, _, name = self.repo.partition("/")
        if not owner or not name:
            raise ValueError(
                f"workspace.repo {self.repo!r} must be 'owner/name' (or a template like "
                "'{repo}'). Without the slash, Factorial cannot mint a token scoped to that "
                "repository."
            )
        return self


#: The E2B session cap (1h) less the adapter's 5m margin: a preview must be parked before E2B's
#: own timeout kills its sandbox (tech-stack decision 54).
MAX_PREVIEW_AWAKE: Final = timedelta(minutes=55)
#: Names the preview's own processes need, which a declared `env` must not replace.
_RESERVED_PREVIEW_ENV: Final = frozenset({"PORT", "HOME", "USER", "PATH", "LANG"})
_ENV_NAME: Final = re.compile(r"[A-Z_][A-Z0-9_]*")
_DEFAULT_STOP_AFTER: Final = timedelta(minutes=30)


class ReadyCheck(_CamelBase):
    """When a service counts as serving: an HTTP answer below 500 on `path`, on 127.0.0.1."""

    path: str = "/"
    timeout: Duration = timedelta(seconds=120)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.path.startswith("/"):
            raise ValueError(f"ready.path {self.path!r} must start with /.")
        if len(self.path) > 256 or any(ord(c) <= 0x20 or ord(c) == 0x7F for c in self.path):
            raise ValueError("ready.path must be at most 256 characters, with no whitespace.")
        if not timedelta(seconds=1) <= self.timeout <= timedelta(minutes=30):
            raise ValueError("ready.timeout must be from 1s to 30m.")
        return self


class PreviewService(_CamelBase):
    """One process the run's sandbox serves: `command` runs as the preview's own uid in the
    checkout, with `PORT` set to `port`."""

    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    command: str = Field(min_length=1, max_length=4096)
    port: int = Field(ge=1024, le=65535)
    ready: ReadyCheck = Field(default_factory=ReadyCheck)

    @field_validator("port")
    @classmethod
    def _not_the_control_plane(cls, value: int) -> int:
        if value == CONTROL_PLANE_PORT:
            raise ValueError(f"Port {value} is reserved for the sandbox's control plane.")
        return value


#: Valid characters in a preview path (and a retired page branch) after placeholders are removed.
#: A path reaches the agent's first turn and a GitHub URL as written, so stray braces are refused
#: by stripping in one pass; leading dashes and trailing slashes are refused explicitly.
_PAGE_TEXT: Final = re.compile(r"[A-Za-z0-9._/-]*")
_PAGE_PLACEHOLDERS: Final = frozenset({"ticket", "slug"})


def _check_ref_text(key: str, value: str, *, pattern: bool = False) -> None:
    """A branch or path a preview names: `{ticket}` and `{slug}` only, and safe characters, `*`
    too when it may be a `pattern`. `key` names the field in the message."""
    # Before the character check, so {repo} is named rather than reported as stray braces.
    if unknown := sorted(set(placeholders(value)) - _PAGE_PLACEHOLDERS):
        named = ", ".join(f"{{{name}}}" for name in unknown)
        raise ValueError(f"{key} may use only {{ticket}} and {{slug}}, not {named}.")
    text = re.sub(r"\{(?:ticket|slug)\}", "", value)
    if not _PAGE_TEXT.fullmatch(text.replace("*", "") if pattern else text):
        allowed = "'.', '_', '-', '/', '*'" if pattern else "'.', '_', '-', '/'"
        raise ValueError(
            f"{key} {value!r} may hold only letters, digits, {allowed} and placeholders."
        )
    if any(segment.startswith("-") for segment in value.split("/")):
        raise ValueError(f"{key} {value!r} must not have a segment starting with '-'.")
    if any("**" in segment and segment != "**" for segment in value.split("/")):
        raise ValueError(
            f'{key} {value!r} has "**" inside a segment. "**" must be a whole segment, '
            'as in "docs/**/*.md".'
        )


def _check_repo_path(key: str, value: str, *, pattern: bool = False) -> None:
    """`_check_ref_text`, and relative inside the repository."""
    _check_ref_text(key, value, pattern=pattern)
    parts = value.split("/")
    if value.startswith("/") or ".." in parts or "" in parts:
        raise ValueError(f"{key} {value!r} must be a relative path inside the repository.")
    # GitHub's paths never hold one, so a declared file with one would never match.
    if pattern and "." in parts:
        raise ValueError(
            f'{key} {value!r} must not have a "." segment. Write it from the repository\'s root.'
        )


class PreviewPage(_CamelBase):
    """`preview.page`, retired: a summary pushed to a branch of its own. The summary is a declared
    file now (`preview.files`, tech-stack decision 71). It still parses, because pinned snapshots
    carry it, and keeps the shape it always had, so no file that was valid becomes invalid;
    nothing reads it, and `config/lint.py` warns a config that still says it."""

    branch: str = Field(min_length=1, max_length=255)
    path: str = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def _check(self) -> Self:
        # Two tickets sharing a page branch would show each other's page.
        if "{ticket}" not in self.branch:
            raise ValueError("preview.page.branch must contain {ticket}.")
        _check_ref_text("preview.page.branch", self.branch)
        if self.branch.endswith("/"):
            raise ValueError(f"preview.page.branch {self.branch!r} must not end in '/'.")
        _check_repo_path("preview.page.path", self.path)
        return self


class PreviewFile(_CamelBase):
    """One of `preview.files`: a file the step is expected to produce on its own branch, shown in
    its Preview section and served inert by the API (tech-stack decisions 58 and 71), a summary
    as much as a spec. Display only: a file the step did not produce shows as missing and blocks
    nothing; `file_exists` in the postconditions is what requires one. What the file holds is
    the agent's instructions' to say: its run is told only the path, the label and how the file
    is served.

    A path with `*` is a pattern, under standard glob rules (`predicates/globs.py`): it shows each
    matching file the step changed, and its label, which it must have, names the set."""

    path: str = Field(min_length=1, max_length=1024)
    label: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def _check(self) -> Self:
        _check_repo_path("preview.files.path", self.path, pattern=True)
        if self.is_pattern and self.label is None:
            raise ValueError(f"preview.files.path {self.path!r} is a pattern, so it needs a label.")
        return self

    @property
    def is_pattern(self) -> bool:
        return "*" in self.path

    @property
    def display_label(self) -> str:
        """The tab's name, and the name the step's agent is told: the file name when unlabelled."""
        return self.label or self.path.rsplit("/", 1)[-1]


class Preview(_CamelBase):
    """`preview:` — a URL for each run of this step, shown beside the run's activity. The URL is
    informational: its absence never blocks the step or changes whether it succeeded.

    With `services`, the run's own sandbox serves the preview (tech-stack decision 54), and
    `setup`, `env` and `stopAfter` configure it. Without, a producer outside the orchestrator
    publishes the URL. With `files`, the API serves files the step's agent commits on its own
    branch (tech-stack decisions 58 and 71). `reviewGate` is retired: Request changes is offered
    at every gate whose `onReject` reruns an agent step (`ProcessConfig.rerun_agent_step`). It
    still parses, because pinned snapshots carry it, and `_check_graph` still holds it to the
    shape it always had, so no file that was valid becomes invalid; nothing reads it. `page` is
    retired the same way (`PreviewPage`).
    """

    enabled: bool = True
    review_gate: str | None = None
    setup: tuple[str, ...] = ()
    services: tuple[PreviewService, ...] = ()
    #: May reference `{secrets.KEY}`. The values reach agent-written code: preview-only values.
    env: dict[str, str] = Field(default_factory=dict)
    stop_after: Duration = _DEFAULT_STOP_AFTER
    page: PreviewPage | None = None
    files: tuple[PreviewFile, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.page is not None and self.services:
            raise ValueError(
                "preview.page and preview.services cannot be combined, because a run frames "
                "one preview."
            )
        paths = [declared.path for declared in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("preview.files paths repeat.")
        if not self.services:
            # By value, not by presence: a pinned snapshot dumps the default back out.
            if self.setup or self.env or self.stop_after != _DEFAULT_STOP_AFTER:
                raise ValueError("preview.setup, env and stopAfter need services.")
            return self
        names = [service.name for service in self.services]
        if len(set(names)) != len(names):
            raise ValueError("Preview service names repeat.")
        ports = [service.port for service in self.services]
        if len(set(ports)) != len(ports):
            raise ValueError("Preview service ports repeat.")
        for key in self.env:
            if not _ENV_NAME.fullmatch(key):
                raise ValueError(f"preview.env name {key!r} is not an environment variable name.")
            if key in _RESERVED_PREVIEW_ENV:
                raise ValueError(f"preview.env name {key!r} is reserved.")
        referenced = {ref for value in self.env.values() for ref in SECRET_REF.findall(value)}
        if unsafe := sorted(ref for ref in referenced if not ref.startswith("PREVIEW_")):
            # The values land in plain text in agent-written code on a public sandbox (design.md
            # §7.10). The name makes "preview-only" something an operator chose when storing it.
            raise ValueError(
                f"preview.env may reference only PREVIEW_ secrets, not {', '.join(unsafe)}."
            )
        if not timedelta(minutes=1) <= self.stop_after <= MAX_PREVIEW_AWAKE:
            raise ValueError("preview.stopAfter must be from 1m to 55m.")
        return self

    @property
    def served_by_runtime(self) -> bool:
        return bool(self.services)

    @property
    def only_a_retired_page(self) -> bool:
        """A block pinned with a retired `page` and nothing else to show. It never waited for an
        outside URL, so it is no preview at all rather than one that waits for ever."""
        return self.page is not None and not self.files and not self.services


class AgentStep(_CamelBase):
    name: str = Field(min_length=1)
    kind: Literal["agent"]
    description: str | None = None
    agent: str = Field(min_length=1)
    next: str = Field(min_length=1)
    workspace: Workspace
    preconditions: Condition | None = None
    inputs: tuple[InputRef, ...] = ()
    #: Applied when an attempt actually SPAWNS (preconditions held, the row won its race, the
    #: adapter accepted) — the issue otherwise learns nothing until the run ends. Fires once per
    #: attempt, so a retry announces itself; gets `{step}` and `{attempt}` beyond the tick's own
    #: values.
    on_start: Outcome | None = None
    postconditions: Condition | None = None
    on_success: Outcome | None = None
    # Per-step overrides of `defaults`; unset means "inherit".
    timeout: Duration | None = None
    retry: Retry | None = None
    on_failure: OnFailure | None = None
    preview: Preview | None = None


#: The closed set of registered action names (design.md §4). Declaration only —
#: `orchestrator.actions` implements them and its registry asserts at import that the two tables
#: agree, so a config naming an unregistered action fails at parse time rather than at dispatch.
ACTION_NAMES: Final[frozenset[str]] = frozenset({"fix_the_contract"})


class ActionStep(_CamelBase):
    """`kind: action` — dispatched to a registered action, not an agent or a human gate.

    No `workspace`/`agent`/`inputs`: an action has no container and no prompt, so it cannot fill
    `RunSpec`'s required `prompts`/`model` fields and never goes through `RuntimeAdapter`.
    """

    name: str = Field(min_length=1)
    kind: Literal["action"]
    description: str | None = None
    action: str = Field(min_length=1)
    next: str = Field(min_length=1)
    on_success: Outcome | None = None
    # Per-step overrides of `defaults`; unset means "inherit", the same convention `AgentStep`
    # uses so `worker.tick_context` handles either kind identically.
    retry: Retry | None = None
    on_failure: OnFailure | None = None
    preview: Preview | None = None

    @model_validator(mode="after")
    def _check_registry(self) -> Self:
        if self.action not in ACTION_NAMES:
            raise ValueError(
                f"Unknown action {self.action!r}. Allowed actions: "
                f"{', '.join(sorted(ACTION_NAMES))}."
            )
        return self


#: A repository role whose holders may decide a gate without being named. Each role includes
#: the ones above it, so `write` also admits maintainers and admins (design.md §7.12).
ApproverPermission = Literal["write", "maintain", "admin"]


class PrRef(_CamelBase):
    head: str
    base: str


class GateApprove(Outcome):
    """`gate.onApprove` — an outcome plus the one action a gate may take itself."""

    merge: bool = False


class GateReject(Outcome):
    """`gate.onReject` — an outcome plus the loop back to a rework step."""

    rerun_step: str = Field(min_length=1)
    inputs: tuple[InputRef, ...] = ()


class Gate(_CamelBase):
    """Every gate control lives here: what is gated, and what happens (design.md §4)."""

    type: Literal["pr_approval"]
    pr: PrRef
    approvers: tuple[str, ...] = ()
    #: Unset in every snapshot pinned before the key existed, which keeps their meaning. Once set,
    #: an empty `approvers` no longer means "anyone".
    approver_permission: ApproverPermission | None = None
    waiting: Outcome | None = None
    on_approve: GateApprove
    on_reject: GateReject | None = None


class GateStep(_CamelBase):
    name: str = Field(min_length=1)
    kind: Literal["human_gate"]
    description: str | None = None
    next: str = Field(min_length=1)
    gate: Gate


class CheckStep(_CamelBase):
    """`kind: check` — completes when its conditions hold on observed state.

    No agent, no action, no human: the step IS its acceptance gate. Exists for work the
    orchestrator can only watch — CI finishing on a pull request — where an agent step would
    spend a container run on waiting and an action has nothing to do. Waiting is attempt-free
    (the same wait-not-fail semantics as agent preconditions); only `timeout` consumes an
    attempt, feeding the ordinary retry/escalate ladder, because a predicate like `ci_green`
    cannot tell "red" from "still running" and only time can.
    """

    name: str = Field(min_length=1)
    kind: Literal["check"]
    description: str | None = None
    next: str = Field(min_length=1)
    conditions: Condition
    #: The PR this step is about, for `{pr_url}` in `onSuccess` — the same shape and matcher
    #: semantics as `gate.pr`. Optional: a check step not about a PR renders no `{pr_url}`.
    pr: PrRef | None = None
    on_success: Outcome | None = None
    # Per-step overrides of `defaults`; unset means "inherit", as on the other kinds.
    timeout: Duration | None = None
    retry: Retry | None = None
    on_failure: OnFailure | None = None
    preview: Preview | None = None


Step = Annotated[AgentStep | GateStep | ActionStep | CheckStep, Field(discriminator="kind")]


# --------------------------------------------------------------------------- process

#: A runtime row's slug: what a YAML author can type unquoted and a URL path can carry unescaped.
#: The registry (`factorial.runtimes.SLUG_RE`) and the process builder read this one.
RUNTIME_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class Entry(_CamelBase):
    """Admission: which tickets enter, and at which step."""

    when: Condition
    step: str = Field(min_length=1)


class TerminalActions(Outcome):
    close_ticket: bool = False


class Terminal(_CamelBase):
    step: str = Field(min_length=1)
    actions: TerminalActions = TerminalActions()


class ProcessConfig(_CamelBase):
    """A parsed process file. A ticket pins one snapshot at admission and keeps it until done;
    the snapshot is identified by the commit it was read from, never by a number in the file."""

    spec_version: SpecVersion
    kind: Literal["Process"]
    name: str = Field(min_length=1)
    #: Accepted and never read. A process is identified by the commit it was synced from, so an
    #: integer an author maintained by hand decided nothing and could disagree with the sha. The
    #: FIELD has to stay declared: `extra="forbid"` plus a snapshot that still carries `version:`
    #: would fail to re-parse on every tick and quarantine the ticket (decision 31).
    version: int | None = None
    description: str | None = None
    roles: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    defaults: Defaults = Defaults()
    #: Absent means `advisory`, which is exactly the behaviour that existed before this field
    #: did — silence must not become stricter, or adding it would change what every
    #: already-pinned process version means.
    policy: Policy = Policy()
    #: The operator-defined Runtime this process runs on, by slug (design.md §7.5's shape:
    #: this file selects, the registry row in Postgres defines). Absent means the deployment
    #: default, so every snapshot pinned before the field existed keeps its meaning.
    runtime: str | None = None
    entry: Entry
    steps: tuple[Step, ...] = Field(min_length=1)
    terminal: Terminal

    @property
    def step_names(self) -> tuple[str, ...]:
        return tuple(step.name for step in self.steps)

    def find_step(self, name: str | None) -> AgentStep | GateStep | ActionStep | CheckStep | None:
        return next((step for step in self.steps if step.name == name), None)

    def rerun_agent_step(self, gate_name: str | None) -> AgentStep | None:
        """The agent step a rejection at `gate_name` sends work back to: its
        `onReject.rerunStep`, when an agent runs it. What a dashboard change request at that gate
        reruns (design.md §7.8). None for a step that is not a gate, a gate with no `onReject`,
        or one whose rerun step no agent runs."""
        gate = self.find_step(gate_name)
        if not isinstance(gate, GateStep) or gate.gate.on_reject is None:
            return None
        target = self.find_step(gate.gate.on_reject.rerun_step)
        return target if isinstance(target, AgentStep) else None

    def step(self, name: str) -> AgentStep | GateStep | ActionStep | CheckStep:
        found = self.find_step(name)
        if found is None:
            raise KeyError(name)
        return found

    @field_validator("runtime")
    @classmethod
    def _check_runtime_slug(cls, value: str | None) -> str | None:
        if value is not None and not RUNTIME_SLUG_RE.match(value):
            raise ValueError(
                f"Runtime {value!r} is not a slug. Use lowercase letters, digits and dashes, "
                "starting with a letter or digit, at most 63 characters."
            )
        return value

    @model_validator(mode="after")
    def _check_graph(self) -> Self:
        """Every pointer resolves, names are unique, and the terminal step is reachable."""
        problems: list[str] = []

        by_name: dict[str, AgentStep | GateStep | ActionStep | CheckStep] = {}
        for step in self.steps:
            if step.name in by_name:
                problems.append(f"Duplicate step name {step.name!r}.")
            by_name[step.name] = step

        if self.terminal.step in by_name:
            problems.append(
                f"Terminal step {self.terminal.step!r} also appears in steps[]. The terminal "
                "step is virtual and must have its own name."
            )
        targets = set(by_name) | {self.terminal.step}

        if self.entry.step not in by_name:
            problems.append(f"entry.step {self.entry.step!r} is not a step.")

        problems.extend(_review_placement_problems(self.entry.when, self.steps))

        for step in self.steps:
            if step.next not in targets:
                problems.append(f"Step {step.name!r}: next {step.next!r} is not a step.")
            if isinstance(step, GateStep) and step.gate.on_reject is not None:
                rerun = step.gate.on_reject.rerun_step
                if rerun not in by_name:
                    problems.append(
                        f"Step {step.name!r}: gate.onReject.rerunStep {rerun!r} is not a step."
                    )
            preview = None if isinstance(step, GateStep) else step.preview
            if preview is not None and preview.review_gate is not None:
                problems.extend(_review_gate_problems(step.name, preview.review_gate, by_name))
            if preview is not None and preview.services and not isinstance(step, AgentStep):
                problems.append(
                    f"Step {step.name!r}: preview.services needs an agent step. An action or "
                    "check step has no sandbox to serve from."
                )
            if preview is not None and preview.page is not None:
                if not isinstance(step, AgentStep):
                    problems.append(
                        f"Step {step.name!r}: preview.page needs an agent step, because only an "
                        "agent pushes a page."
                    )
                elif preview.page.branch == step.workspace.base:
                    problems.append(
                        f"Step {step.name!r}: preview.page.branch must not be workspace.base "
                        f"{step.workspace.base!r}. A page never goes to the branch that work "
                        "merges into."
                    )
                elif preview.page.branch == step.workspace.branch:
                    problems.append(
                        f"Step {step.name!r}: preview.page.branch {preview.page.branch!r} is the "
                        "step's own workspace.branch. A page lives on a branch of its own and "
                        "never merges."
                    )
                else:
                    problems.extend(_page_branch_overlaps(step, preview.page.branch, by_name))
            if preview is not None and preview.files:
                if not isinstance(step, AgentStep):
                    problems.append(
                        f"Step {step.name!r}: preview.files needs an agent step, because only "
                        "an agent run's branch is read."
                    )
                elif "{ticket}" not in step.workspace.branch:
                    problems.append(
                        f"Step {step.name!r}: preview.files needs {{ticket}} in "
                        "workspace.branch. Without it, the branch could be another ticket's."
                    )

        if not problems:
            reached = _reachable(by_name, self.entry.step)
            if self.terminal.step not in reached:
                problems.append(
                    f"Terminal step {self.terminal.step!r} is unreachable from entry step "
                    f"{self.entry.step!r}."
                )
            orphans = sorted(set(by_name) - reached)
            if orphans:
                problems.append(
                    f"Step(s) {', '.join(repr(n) for n in orphans)} are unreachable from entry "
                    f"step {self.entry.step!r}."
                )

        if problems:
            raise ValueError(" ".join(problems))
        return self


def _review_placement_problems(
    entry_when: Condition, steps: tuple[AgentStep | GateStep | ActionStep | CheckStep, ...]
) -> list[str]:
    """`request_approval()` is fed its approval only when an agent step's run is accepted, so
    anywhere else it reads false for ever, and under `not` an approval would fail the run."""
    places = ["entry.when"] if awaits_review(entry_when) else []
    for step in steps:
        if isinstance(step, AgentStep):
            misplaced = awaits_review(step.preconditions) or _negates_review(step.postconditions)
        elif isinstance(step, CheckStep):
            misplaced = awaits_review(step.conditions)
        else:
            misplaced = False
        if misplaced:
            places.append(f"Step {step.name!r}")
    return [
        f"{where}: {REVIEW_PREDICATE}() may appear only in an agent step's postconditions, "
        "and never under not."
        for where in places
    ]


def _negates_review(condition: Condition | None) -> bool:
    match condition:
        case Not():
            return awaits_review(condition.not_)
        case AllOf():
            return any(_negates_review(child) for child in condition.all_of)
        case AnyOf():
            return any(_negates_review(child) for child in condition.any_of)
        case _:
            return False


def _page_branch_overlaps(
    step: AgentStep,
    page_branch: str,
    by_name: Mapping[str, AgentStep | GateStep | ActionStep | CheckStep],
) -> list[str]:
    """A page branch that a step's delivery probe (`spec/42-*`) also finds makes that step's
    `{slug}` ambiguous for the whole ticket, and can satisfy its `branch_exists` on the page
    alone. Both names read every placeholder as `*`, and either glob matching the other is an
    overlap: that catches every name that extends or specialises a step branch."""
    page = globbed(page_branch)
    for other in by_name.values():
        if not isinstance(other, AgentStep):
            continue
        glob = globbed(other.workspace.branch)
        if fnmatchcase(page, glob) or fnmatchcase(glob, page):
            return [
                f"Step {step.name!r}: preview.page.branch {page_branch!r} overlaps step "
                f"{other.name!r}'s workspace.branch {other.workspace.branch!r}. A page branch "
                "must be named apart from every step branch."
            ]
    return []


def _review_gate_problems(
    step: str, gate_name: str, by_name: Mapping[str, AgentStep | GateStep | ActionStep | CheckStep]
) -> list[str]:
    """A change request rejects `gate_name` and expects the rework to land back on `step`, whose
    preview the reviewer was looking at. Any other shape starts the new attempt somewhere the
    reviewer's feedback does not describe, so the parser refuses it rather than leaving the loop
    to guess."""
    where = f"Step {step!r}: preview.reviewGate {gate_name!r}"
    gate = by_name.get(gate_name)
    if not isinstance(gate, GateStep):
        return [f"{where} is not a human_gate step."]
    problems: list[str] = []
    on_reject = gate.gate.on_reject
    if on_reject is None or on_reject.rerun_step != step:
        problems.append(
            f"{where} must send rework back to {step!r}. Its gate.onReject.rerunStep is "
            f"{None if on_reject is None else on_reject.rerun_step!r}."
        )
    seen = {step}
    current = by_name[step].next
    while current not in seen and current != gate_name and current in by_name:
        seen.add(current)
        current = by_name[current].next
    if current != gate_name:
        problems.append(f"{where} is not reached from {step!r} by following next.")
    return problems


def _reachable(
    by_name: Mapping[str, AgentStep | GateStep | ActionStep | CheckStep], start: str
) -> set[str]:
    """Steps reachable from `start` via `next` and gate rework edges."""
    seen = {start}
    queue = deque([start])
    while queue:
        step = by_name.get(queue.popleft())
        if step is None:
            continue
        edges = [step.next]
        if isinstance(step, GateStep) and step.gate.on_reject is not None:
            edges.append(step.gate.on_reject.rerun_step)
        for target in edges:
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return seen


# ---------------------------------------------------------------------- agent manifest

#: Size aliases resolve to names the API rejects, so the agent dies on its first turn.
#: Rejected here rather than in the adapter: a manifest that cannot possibly run is invalid
#: config, whichever runtime reads it.
_SIZE_ALIASES: Final[frozenset[str]] = frozenset(
    {"opus", "sonnet", "haiku", "claude-opus", "claude-sonnet", "claude-haiku"}
)


class Prompts(_Base):
    """Paths relative to the manifest's folder. Prompts are markdown files, never inline YAML."""

    system: str
    instructions: str


def _unique_across_scopes(field: str, local: tuple[str, ...], shared: tuple[str, ...]) -> None:
    """A skill or server is addressed by name in the container, so only one file may claim it."""
    names = [*local, *shared]
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        raise ValueError(
            f"{', '.join(repeated)} declared more than once across {field}.local and "
            f"{field}.shared. A name must be unique across scopes."
        )


class Skills(_Base):
    local: tuple[str, ...] = ()  # from this agent's skills/ directory
    shared: tuple[str, ...] = ()  # from the .factorial-level skills/ directory

    @model_validator(mode="after")
    def _check_unique(self) -> Self:
        _unique_across_scopes("skills", self.local, self.shared)
        return self


class Limits(_Base):
    """Run caps, kept nested so the manifest stays runtime-neutral."""

    #: Accepted and never read. A session renews its token every hour, so nothing bounds on it.
    #: The FIELD has to stay declared: `extra="forbid"` would otherwise refuse fleet manifests
    #: and pinned snapshots that still carry it.
    max_duration: Duration | None = None
    max_turns: int | None = Field(default=None, ge=1)


class SecretDecl(_Base):
    """What must be minted for this agent — a manifest, not an isolation boundary (§7.4)."""

    key: str = Field(min_length=1)
    description: str | None = None


class Permissions(_Base):
    """Requested GitHub App permission scopes for this agent, scope name -> `read` | `write`.

    Named fields plus `extra="forbid"` are what make `workflows` structurally undeclarable:
    CI runs agent-authored code with repo secrets before any human sees it, so the App must
    never hold `workflows: write` and a manifest must not be able to ask for it (design.md §7.2).

    No `permissions:` key at all means the default profile (`github/client.py`'s
    `APP_PERMISSIONS`), never "nothing". Shape only — enforcement against a project's ceiling
    lives in `orchestrator.factorial.permission_policy`.
    """

    contents: Literal["read", "write"] | None = None
    issues: Literal["read", "write"] | None = None
    pull_requests: Literal["read", "write"] | None = None
    metadata: Literal["read", "write"] | None = None
    checks: Literal["read", "write"] | None = None
    actions: Literal["read", "write"] | None = None


# ------------------------------------------------------------------------ mcp servers

#: `{secrets.KEY}` — the only substitution allowed in an MCP header or env value, so a server
#: manifest can name a credential without holding one. `runtime.contract.McpServerSpec`'s
#: `rendered_headers` / `rendered_env` expand it from the secrets injected at spawn, so no value
#: ever lives in `.factorial/`, an event, or a `RunSpec` field. The spelling is duplicated in the
#: contract because the orchestrator validates it and the runtime renders it, and neither may
#: import the other (design.md §7.5).
SECRET_REF: Final = re.compile(r"\{secrets\.([A-Za-z_][A-Za-z0-9_]*)\}")

#: A server is addressed by name inside the container and in tool names
#: (`mcp__<server>__<tool>`), so the name has to survive both.
_MCP_NAME: Final = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: A stdio server runs a command beside the run's live `GH_TOKEN`. A floating version changes
#: the code running there with no config change and no new pin.
_FLOATING_VERSIONS: Final[frozenset[str]] = frozenset(
    {"latest", "next", "canary", "beta", "alpha", "dev", "main", "master", "edge", "*"}
)


class McpHttp(_Base):
    """`transport: http | sse`. HTTPS only: headers usually carry a credential."""

    url: str = Field(min_length=1)
    #: Values may contain `{secrets.KEY}` references; see `SECRET_REF`.
    headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_scheme(self) -> Self:
        if not self.url.lower().startswith("https://"):
            raise ValueError(
                f"MCP server URL {self.url!r} must use https://, because its headers carry "
                "credentials."
            )
        return self


class McpStdio(_Base):
    """`transport: stdio`. Arbitrary code in the agent's container; versions must be pinned."""

    command: str = Field(min_length=1)
    args: tuple[str, ...] = ()
    #: Values may contain `{secrets.KEY}` references; see `SECRET_REF`.
    env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_pinned(self) -> Self:
        floating = [arg for arg in self.args if _is_floating(arg)]
        if floating:
            raise ValueError(
                f"stdio arg(s) {', '.join(repr(arg) for arg in floating)} name a floating "
                "version. Pin an exact one, for example '@acme/mcp@1.4.2'. A floating tag changes "
                "the code running beside a live GH_TOKEN without changing any pinned config."
            )
        return self


def _is_floating(arg: str) -> bool:
    """`@acme/pkg@latest`, `pkg@main` — a version specifier that is not a version."""
    _, sep, version = arg.rpartition("@")
    if not sep or not version:
        return False
    return version.strip().lower() in _FLOATING_VERSIONS


class McpTools(_Base):
    """Which of a server's tools the agent may call, as `fnmatch` patterns.

    Empty `allow` means every tool the server exposes; the project's operator allowlist still has
    to permit it. On the claude-cli substrate tool narrowing is best-effort — server-level
    containment is the structural part (design.md §7.5).
    """

    allow: tuple[str, ...] = ()


class McpServerManifest(_Base):
    """A parsed `.factorial/mcp/<name>.yaml` (or an agent-local `mcp/<name>.yaml`).

    Runtime-neutral like `agent.yaml`: what the server is, never how a substrate wires it up.
    The file stem must equal `name` (checked by both `loader` and `factorial.source`) — that name is
    what an agent references and what the tool prefix is built from.
    """

    spec_version: SpecVersion = Field(alias="specVersion")
    kind: Literal["McpServer"]
    name: str = Field(min_length=1)
    description: str | None = None
    transport: Literal["http", "sse", "stdio"]
    http: McpHttp | None = None
    stdio: McpStdio | None = None
    #: What must be minted for this server — a manifest, not a boundary, as in `agent.yaml`.
    secrets: tuple[SecretDecl, ...] = ()
    tools: McpTools = McpTools()
    #: `true` (default): a server that will not connect fails the run with a reason. `false`: the
    #: run proceeds without it. Degrading silently makes a config bug look like a bad prompt.
    required: bool = True

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not _MCP_NAME.match(self.name):
            raise ValueError(
                f"MCP server name {self.name!r} must be lowercase letters, digits and dashes, "
                "because it becomes a tool prefix (`mcp__<server>__<tool>`)."
            )
        if self.transport == "stdio":
            if self.stdio is None:
                raise ValueError("The transport is 'stdio', but no `stdio:` block was given.")
            if self.http is not None:
                raise ValueError("The transport is 'stdio', so an `http:` block does not apply.")
        else:
            if self.http is None:
                raise ValueError(
                    f"The transport is {self.transport!r}, but no `http:` block was given."
                )
            if self.stdio is not None:
                raise ValueError(
                    f"The transport is {self.transport!r}, so a `stdio:` block does not apply."
                )

        undeclared = sorted(set(self.secret_refs()) - {decl.key for decl in self.secrets})
        if undeclared:
            raise ValueError(
                f"Value(s) reference undeclared secret(s) {', '.join(undeclared)}. Add them to "
                "this server's `secrets:` block. Factorial resolves only that list at spawn, so "
                "an undeclared reference reaches the runtime unrendered."
            )
        return self

    def _templated_values(self) -> tuple[str, ...]:
        if self.http is not None:
            return (self.http.url, *self.http.headers.values())
        if self.stdio is not None:
            return tuple(self.stdio.env.values())
        return ()  # unreachable: the validator above requires exactly one block

    def secret_refs(self) -> tuple[str, ...]:
        """Secret keys named by `{secrets.KEY}` anywhere in this server, first-seen order."""
        seen: dict[str, None] = {}
        for value in self._templated_values():
            for match in SECRET_REF.finditer(value):
                seen.setdefault(match.group(1), None)
        return tuple(seen)


class McpRefs(_Base):
    """Which servers an agent gets, by name — the same local/shared split as `skills:`.

    Names only: narrowing tools per agent would put one policy in two files that can disagree,
    so a server needing a different tool set is a different server file.
    """

    local: tuple[str, ...] = ()  # from this agent's mcp/ directory
    shared: tuple[str, ...] = ()  # from the .factorial-level mcp/ directory

    @model_validator(mode="after")
    def _check_unique(self) -> Self:
        _unique_across_scopes("mcp", self.local, self.shared)
        return self


class AgentManifest(_Base):
    """A parsed `agents/<name>/agent.yaml`. Folder name must equal `name` (loader checks)."""

    spec_version: SpecVersion = Field(alias="specVersion")
    kind: Literal["Agent"]
    name: str = Field(min_length=1)
    description: str | None = None
    prompts: Prompts
    skills: Skills = Skills()
    #: MCP servers this agent may call, by name (design.md §7.5). Absent means none.
    mcp: McpRefs = McpRefs()
    model: str = Field(min_length=1)
    limits: Limits
    secrets: tuple[SecretDecl, ...] = ()
    #: Requested GitHub App permission scopes, checked against the project's operator-owned
    #: `permission_ceiling` on every resolution. Absent means the default profile, not nothing
    #: — see `Permissions`.
    permissions: Permissions | None = None
    #: Ignored: the process's Runtime says which CLI runs the agent. Accepted so a manifest that
    #: still has the block parses.
    runtime: Any = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _check_model_id(self) -> Self:
        model = self.model.strip()
        if (
            model.lower() in _SIZE_ALIASES
            or model.endswith("-latest")
            or not any(char.isdigit() for char in model)
        ):
            raise ValueError(
                f"Model {self.model!r} looks like a size alias. Use a concrete, versioned model "
                "ID, for example 'claude-haiku-4-5-20251001'. An alias resolves to a name the "
                "API rejects, and the agent fails on its first turn."
            )
        return self
