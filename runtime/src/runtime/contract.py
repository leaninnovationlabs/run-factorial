"""Vendor-neutral agent-runtime contract (design.md §3.2).

Nothing here may assume a shape of substrate: no subprocesses, no CLIs, no HTTP, no file paths
standing in for streams. The CLI adapters and the planned HTTP adapter (tech-stack.md §5) must be
interchangeable without anything above the adapter changing.

Scope: checkout, secret injection, result reporting. Labels, promotion, merging, and postcondition
verification are the orchestrator's and have no representation here.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from collections.abc import AsyncIterator, Collection, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar, Final, Literal, Protocol, runtime_checkable

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_serializer,
    field_validator,
    model_validator,
)

__all__ = [
    "ATTACHMENT_EXTENSIONS",
    "CONTROL_PLANE_PORT",
    "PAGE_MAX_BYTES",
    "PREVIEW_CONTINUED",
    "PREVIEW_IDLE",
    "PREVIEW_RENEWAL",
    "REDACTED",
    "SELECTOR_HEAD_GLOB",
    "SELECTOR_MARKER",
    "SELECTOR_WHERE",
    "WHERE_ISSUE",
    "WHERE_PR",
    "AdapterUnavailable",
    "AgentPrompts",
    "AgentRuntimeError",
    "AgentSkill",
    "Artifact",
    "ArtifactKind",
    "AttachmentMediaType",
    "CredentialSweeper",
    "ExpectedArtifact",
    "IdleReason",
    "InspectHost",
    "InspectLoginRefused",
    "InspectNoSession",
    "InspectPhase",
    "InspectState",
    "InspectUnavailable",
    "IsolationError",
    "McpServerSpec",
    "McpTransport",
    "MissingAttachment",
    "NoBranchHeld",
    "ParkReason",
    "PreviewHost",
    "PreviewPhase",
    "PreviewServiceSpec",
    "PreviewSpec",
    "PreviewState",
    "PreviewUnavailable",
    "PreviewUrl",
    "ResumeFrom",
    "ResumeMode",
    "RunAttachment",
    "RunHandle",
    "RunInput",
    "RunResult",
    "RunSpec",
    "RunStatus",
    "RunUsage",
    "RuntimeAdapter",
    "SecretRoute",
    "SessionState",
    "SessionUnavailable",
    "ShownFile",
    "SkillScope",
    "SpawnError",
    "SpawnReason",
    "SubstrateUnavailable",
    "TerminalTarget",
    "UnknownRunError",
    "Workspace",
    "WorkspaceReaper",
    "missing_expectations",
    "unmet_expectations",
]

#: Placeholder substituted for every secret value on serialization.
REDACTED = "**********"


# --------------------------------------------------------------------------- errors


class AgentRuntimeError(RuntimeError):
    """Base for every adapter failure — catchable without importing an adapter."""


class SpawnReason(StrEnum):
    """Why a spawn was refused — a closed set, because adapter wording is adapter-owned and the
    caller must never sniff a message. `is_permanent` is the orchestrator-facing split.

    Permanent — escalate now; the next attempt reads the same config or the same store:

    - `CONFIG` — the spec asks this substrate for something it will never do: a model size alias,
      an `mcp:` block against an adapter whose `supports_mcp` is False,
      `attachments` against an adapter whose `supports_attachments` is False, a declared
      expectation it has no probe for, `isolation="blind"`, which no adapter runs
      (design.md §7.6), a self-contradicting spec (a credential-less workspace that also
      declares a GitHub secret), or a sandbox the substrate needs that cannot start on this
      machine.
    - `CREDENTIAL` — a credential the run cannot start without is absent, unusable, or refused.
      Only an operator storing or fixing a secret changes that.
    - `WORKSPACE` — the `workspace:` block cannot be realised as written: a `base` ref that does
      not exist, a `base` of the wrong shape for the isolation mode, a repo name that escapes the
      adapter's root.

    Retryable:

    - `INFRASTRUCTURE` — the substrate itself refused or misbehaved (busy, a duplicate container,
      a failed clone, a CLI that exited nonzero). Nothing about the spec is wrong. Also the
      default, so an unclassified refusal keeps the retry behaviour rather than escalating on a
      guess.
    """

    CONFIG = "config"
    CREDENTIAL = "credential"
    WORKSPACE = "workspace"
    INFRASTRUCTURE = "infrastructure"

    @property
    def is_permanent(self) -> bool:
        """`True` when a retry cannot possibly fix this — the caller should escalate now."""
        return self in _PERMANENT_SPAWN_REASONS


#: The reasons that mean "an operator must act", spelled once so the orchestrator and the
#: conformance battery cannot drift on which side of the line a reason sits.
_PERMANENT_SPAWN_REASONS: Final = frozenset(
    {SpawnReason.CONFIG, SpawnReason.CREDENTIAL, SpawnReason.WORKSPACE}
)


class SpawnError(AgentRuntimeError):
    """The run could not be started; nothing was launched.

    Whether a retry can help is `reason.is_permanent` (see `SpawnReason`). The default,
    `INFRASTRUCTURE`, means "may be retried", so an unclassified site is never escalated on a
    guess.
    """

    def __init__(self, *args: object, reason: SpawnReason = SpawnReason.INFRASTRUCTURE) -> None:
        super().__init__(*args)
        #: Never `None` — an unclassified refusal is `INFRASTRUCTURE`, not a third state.
        self.reason: SpawnReason = reason


class SubstrateUnavailable(SpawnError):
    """The substrate's own service could not be reached — rate-limited, down, or timing out.

    Always retryable, and the orchestrator does not count it toward a ticket's quarantine: the
    ticket did nothing wrong, and quarantining the fleet for a vendor's outage would bury a
    transient fault under manual requeues. A run's own failure, such as a checkout that did not
    clone, stays a plain `SpawnError`.
    """

    def __init__(self, *args: object) -> None:
        super().__init__(*args, reason=SpawnReason.INFRASTRUCTURE)


class SessionUnavailable(SpawnError):
    """`RunSpec.resume_from` names a session this substrate can no longer continue: its kept
    workspace was reaped, or its session is gone. Nothing was launched.

    WORKSPACE, so a caller that does not know it escalates; the orchestrator catches it first and
    spawns the rework fresh, because the session only made a new attempt cheaper. A `resume` that
    cannot continue raises `UnknownRunError` instead: a human asked for that same run.
    """

    def __init__(self, *args: object) -> None:
        super().__init__(*args, reason=SpawnReason.WORKSPACE)


class NoBranchHeld(SessionUnavailable):
    """The earlier run ended on no branch, so a continued session has no work to build on. The
    caller starts fresh."""


class UnknownRunError(AgentRuntimeError):
    """The substrate has no record of this run id (expired, purged, or never accepted)."""


class AdapterUnavailable(AgentRuntimeError):
    """The substrate itself is unreachable or misconfigured — not a fault of this run."""


class PreviewUnavailable(AgentRuntimeError):
    """This run's workspace cannot serve a preview: it was not kept, its agent still runs, or
    its workspace is gone."""


class InspectUnavailable(AgentRuntimeError):
    """This run's workspace cannot open a shell: it was not kept, its agent still runs, or its
    workspace is gone. Its message may carry what the substrate printed, so it is logged and
    never reaches an event."""


class InspectLoginRefused(InspectUnavailable):
    """The view's CLI refused the runtime's stored login. Its message is fixed here and never
    substrate text, so unlike its parent's it may reach an event."""

    MESSAGE: Final = "the runtime's stored login was refused: store a fresh one"

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


class InspectNoSession(InspectUnavailable):
    """The run never began a session, as a run from before every run was one did not: there is
    no TUI to reopen. Fixed text, like `InspectLoginRefused`, so it may reach an event."""

    MESSAGE: Final = "this run never began a session, so there is no session to open"

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


class IsolationError(AgentRuntimeError):
    """A `runtime.workspace` gate (`assert_isolated`, `assert_no_retained_credential`) found the
    checkout is not blind — a hidden ref or path is reachable, or a credential was retained."""


# --------------------------------------------------------------------------- values


class RunStatus(StrEnum):
    """Exactly the states design.md §3.2 defines. Adapters map vendor states onto these."""

    QUEUED = "queued"
    RUNNING = "running"
    #: A session alive at its prompt, waiting on a turn (decision 56). Not terminal:
    #: the orchestrator decides from the postconditions whether to `finish` it or wait for a human,
    #: and why it idles is `SessionState.idle_reason`.
    IDLE = "idle"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    #: Stopped by a human who intends to redirect it, NOT a failure the step recovers from
    #: (tech-stack.md decision 26). Distinct from CANCELLED because the two want opposite
    #: treatment: a cancelled run is retried by the loop and its workspace is cleaned up, while an
    #: interrupted one parks its ticket and keeps the workspace, which is what a resume runs in.
    #: Terminal — the process really did stop; what differs is what the orchestrator does next.
    INTERRUPTED = "interrupted"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATUSES


_TERMINAL_STATUSES = frozenset(
    {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.TIMEOUT,
        RunStatus.CANCELLED,
        RunStatus.INTERRUPTED,
    }
)


class ParkReason(StrEnum):
    """Why `interrupt` parks a run (decision 26)."""

    #: A human pressed Interrupt.
    USER = "user"
    #: A session idled past `terminal.idle_park_after` (decision 56).
    IDLE = "idle"
    #: The run's token nears its hour: it parks and resumes on the same tick with a fresh one.
    ROLLOVER = "rollover"
    #: A session lived `terminal.session_ceiling` since its spawn or a dashboard Resume.
    CEILING = "ceiling"
    #: The session ended with nobody asking: a person ended it from its terminal, and the
    #: workspace is kept for a resume.
    EXITED = "exited"


class IdleReason(StrEnum):
    """Why a session is IDLE, as its turn-end hook reported it (decision 56)."""

    TURN_END = "turn_end"
    #: The agent asked the human something; `SessionState.question` carries it.
    QUESTION = "question"
    TURN_ERROR = "turn_error"


class ArtifactKind(StrEnum):
    """What an agent may deliver. Verifiable things only — the orchestrator checks predicates
    against these, never the agent's prose (design.md §3.2)."""

    BRANCH = "branch"
    PR = "pr"
    COMMIT = "commit"
    FILE = "file"
    COMMENT = "comment"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Workspace(_Frozen):
    """Where the agent works. `branch` is the template the agent publishes under and may still
    contain `{slug}`, chosen by the agent at runtime (process.yaml `workspace:`).

    No `path` field (decision S9): a host filesystem path is not a vendor-neutral concept — this
    module must not assume a worktree or a container mount. `isolation` and `hidden` say what the
    run may see, never where; each adapter supplies the mechanism (see
    `runtime.workspace.assert_isolated`, the shared enforcement every adapter calls).
    """

    repo: str = Field(min_length=1, description="owner/name")
    base: str = Field(min_length=1, description="ref to check out from")
    branch: str = Field(min_length=1, description="branch the agent must push")
    #: `"shared"`: the checkout may see anything reachable in the repo. `"blind"`: the run must
    #: not observe `hidden`, and an adapter whose `supports_blind_workspace` is False must refuse
    #: the spawn rather than run it on a shared checkout.
    isolation: Literal["shared", "blind"] = "shared"
    #: What `isolation="blind"` must hide. An entry starting `refs/` is a ref glob (local and
    #: remote refs, tags included); every other entry is a path glob (working-tree contents, and a
    #: git pathspec for the reachable-commit check). Meaningless when `isolation` is `"shared"`.
    hidden: tuple[str, ...] = ()
    #: `"minted"`: a per-run, repo-scoped credential. `"none"`: none injected at all — for a blind
    #: run where even a scoped token would let the agent read what `hidden` hides.
    credential: Literal["minted", "none"] = "minted"
    #: The commit the run's config was pinned at. The checkout's own `.factorial/` may be stale.
    config_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")


class SkillScope(StrEnum):
    """Where the manifest declared a skill (`skills.local` / `skills.shared` in agent.yaml).
    Carried through so a substrate can lay the two out separately; it implies no precedence."""

    LOCAL = "local"
    SHARED = "shared"


class RunInput(_Frozen):
    """One resolved input. The orchestrator has already rendered templates and read files; the
    adapter passes values through and never resolves anything itself."""

    name: str = Field(min_length=1)
    value: str


#: The image types both CLIs attach natively, and the extension an adapter names each file with.
#: Closed: an SVG is XML the model would read as text, and nothing else is an image the model
#: APIs accept.
ATTACHMENT_EXTENSIONS: Final[Mapping[str, str]] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}

AttachmentMediaType = Literal["image/png", "image/jpeg", "image/gif", "image/webp"]


class RunAttachment(_Frozen):
    """One image embedded in the run's input text, already fetched by the orchestrator.

    Bytes, not a path or a URL (decision S9): where the file lands is the adapter's choice, and a
    URL would hand the substrate a fetch the orchestrator already made. `content` never
    serializes, because a `RunSpec` reaches logs, events and error payloads: a dump carries its
    size instead.
    """

    #: The file stem the adapter writes and the prompt names. Generated by the orchestrator,
    #: never taken from a URL or alt text.
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    media_type: AttachmentMediaType
    content: bytes = Field(min_length=1, repr=False)
    #: The input the image was embedded in: `ticket_body`, `ticket_comments`, ...
    source: str = Field(min_length=1)
    #: The link as its author wrote it, with no query string — so never a signed URL.
    origin: str = Field(pattern=r"^[^?#\s]+$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _digest_matches(self) -> RunAttachment:
        if hashlib.sha256(self.content).hexdigest() != self.sha256:
            raise ValueError(f"attachment {self.name!r}: sha256 does not match its content")
        return self

    @field_serializer("content", when_used="always")
    def _size_only(self, content: bytes) -> int:
        return len(content)

    @property
    def filename(self) -> str:
        return f"{self.name}.{ATTACHMENT_EXTENSIONS[self.media_type]}"


class MissingAttachment(_Frozen):
    """An image the input text embeds that the run does not get, and why — so the prompt says
    so, rather than leaving the agent a link it cannot open."""

    source: str = Field(min_length=1)
    origin: str = Field(pattern=r"^[^?#\s]+$")
    reason: str = Field(min_length=1)


class AgentPrompts(_Frozen):
    """Prompt contents, already fetched — not paths.

    The orchestrator reads the manifest's files at the agent's pinned commit sha, so a pinned sha
    pins behaviour rather than whatever the substrate happens to check out.
    """

    system: str = Field(min_length=1, description="an agent with no system prompt is not an agent")
    #: The manifest always declares one, but an empty operational-context file is legitimate.
    instructions: str = ""


class AgentSkill(_Frozen):
    """One resolved skill: the `SKILL.md` text, fetched the same way and at the same sha as the
    prompts. `name` is the skill directory name the manifest referenced."""

    name: str = Field(min_length=1)
    content: str = Field(description="the SKILL.md text, frontmatter included")
    scope: SkillScope


class McpTransport(StrEnum):
    """How a substrate reaches an MCP server. `stdio` runs a command where the agent runs."""

    HTTP = "http"
    SSE = "sse"
    STDIO = "stdio"


#: `{secrets.KEY}` in a header or env value. Duplicated from `orchestrator.config.models` because
#: neither package may import the other; changing the syntax means changing both, and there is no
#: third place.
_SECRET_REF = re.compile(r"\{secrets\.([A-Za-z_][A-Za-z0-9_]*)\}")


class McpServerSpec(_Frozen):
    """One MCP server this run may call (design.md §7.5).

    Carries no credential: header and env values keep their `{secrets.KEY}` references and the
    adapter renders them at spawn, so nothing here needs redacting and `resolved_secrets()` stays
    the only place a value is unwrapped — hence plain `str`, not `SecretStr`.

    `tools_allow` is what the config asked for. A server absent from this tuple cannot be called
    at all; narrowing within a server is enforced only as well as the substrate allows, and each
    adapter documents which it gives you.
    """

    name: str = Field(min_length=1)
    transport: McpTransport
    url: str | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    headers: Mapping[str, str] = Field(default_factory=dict)
    env: Mapping[str, str] = Field(default_factory=dict)
    #: `fnmatch` patterns; empty means every tool the server exposes.
    tools_allow: tuple[str, ...] = ()
    #: `True`: the run fails if the server cannot be reached. `False`: it proceeds without it.
    required: bool = True

    @model_validator(mode="after")
    def _check_shape(self) -> McpServerSpec:
        if self.transport is McpTransport.STDIO:
            if not self.command:
                raise ValueError(f"mcp server {self.name!r}: stdio transport needs a command")
            if self.url:
                raise ValueError(f"mcp server {self.name!r}: stdio transport has no url")
        else:
            if not self.url:
                raise ValueError(
                    f"mcp server {self.name!r}: {self.transport} transport needs a url"
                )
            if self.command or self.args:
                raise ValueError(
                    f"mcp server {self.name!r}: {self.transport} transport has no command/args"
                )
        return self

    def rendered_headers(self, secrets: Mapping[str, str]) -> dict[str, str]:
        """Headers with every `{secrets.KEY}` replaced. Raises `SpawnError` if a key is absent."""
        return {key: self._render(value, secrets) for key, value in self.headers.items()}

    def rendered_env(self, secrets: Mapping[str, str]) -> dict[str, str]:
        """Env with every `{secrets.KEY}` replaced. Raises `SpawnError` if a key is absent."""
        return {key: self._render(value, secrets) for key, value in self.env.items()}

    def _render(self, value: str, secrets: Mapping[str, str]) -> str:
        missing: list[str] = []

        def substitute(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in secrets:
                missing.append(key)
                return match.group(0)
            return secrets[key]

        result = _SECRET_REF.sub(substitute, value)
        if missing:
            # Never send the unrendered template: an `Authorization: Bearer {secrets.X}` header is
            # a 401 at best, and at worst a request that looks authenticated and is not.
            # CREDENTIAL, because only a human storing the secret changes the next attempt.
            raise SpawnError(
                f"MCP server {self.name!r} references secret(s) {', '.join(sorted(set(missing)))} "
                "that were not injected into this run. Store them in Factorial and declare them "
                "in the server's `secrets:` block",
                reason=SpawnReason.CREDENTIAL,
            )
        return result


class Artifact(_Frozen):
    """A delivered thing. `locator` is the kind-specific address (e.g. `{"branch": "spec/12-x"}`,
    `{"number": "34"}`), left loose: the runtime must not know it maps onto the orchestrator's
    `artifact` table."""

    kind: ArtifactKind
    locator: Mapping[str, str] = Field(default_factory=dict)
    url: str | None = None


#: Selector keys, spelled once so the adapters cannot drift on them.
SELECTOR_HEAD_GLOB: Final = "head_glob"
SELECTOR_MARKER: Final = "marker"
SELECTOR_WHERE: Final = "where"
#: `where` values: which conversation carries the comment.
WHERE_ISSUE: Final = "issue"
WHERE_PR: Final = "pr"

#: The closed selector vocabulary: kind -> (required keys, optional keys). A kind with no keys has
#: no shipped probe either, so every adapter refuses such a spawn (see `ExpectedArtifact`).
_SELECTOR_VOCABULARY: Final[Mapping[ArtifactKind, tuple[frozenset[str], frozenset[str]]]] = {
    ArtifactKind.BRANCH: (frozenset(), frozenset({SELECTOR_HEAD_GLOB})),
    ArtifactKind.PR: (frozenset(), frozenset({SELECTOR_HEAD_GLOB})),
    ArtifactKind.COMMENT: (
        frozenset({SELECTOR_MARKER}),
        frozenset({SELECTOR_WHERE, SELECTOR_HEAD_GLOB}),
    ),
    ArtifactKind.COMMIT: (frozenset(), frozenset()),
    ArtifactKind.FILE: (frozenset(), frozenset()),
}


class ExpectedArtifact(_Frozen):
    """One thing this run must deliver, declared by the caller, and how to recognise it.

    Without it an adapter can only probe for the shapes it happens to know about — an agent whose
    entire delivery is a COMMENT leaves nothing a branch/PR probe can see. What counts as
    delivered is the caller's statement, not the adapter's guess. An empty
    `RunSpec.expected_artifacts` means nothing was declared, and every adapter keeps its own
    default probe.

    The selector vocabulary is closed and validated here; an unknown key is a `ValueError` at
    construction rather than a key an adapter silently ignores:

    - `BRANCH` — `head_glob` (optional): an `fnmatch` glob the pushed branch must match, e.g.
      `impl/42-*`. Absent means the branch the run was told to publish (`Workspace.branch`, with
      `{slug}` globbed); the adapter supplies that fallback, being the only party that knows it.
    - `PR` — `head_glob` (optional): the same glob, matched against the pull request's head ref.
    - `COMMENT` — `marker` (required): a substring the comment body must contain. `where`
      (optional, `issue` or `pr`, default `issue`) says which conversation to read; `head_glob`
      (optional, `where=pr` only) says which pull request, defaulting to the run's branch.
    - `COMMIT` and `FILE` — no keys: no shipped adapter can verify either, so the selector must
      be empty and both refuse the spawn rather than pretend. An adapter that grows a probe for
      one accepts it without a contract change.

    Globs are `fnmatch`, the convention every predicate and probe matches `{slug}` with.
    """

    kind: ArtifactKind
    selector: Mapping[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_selector(self) -> ExpectedArtifact:
        required, optional = _SELECTOR_VOCABULARY[self.kind]
        allowed = required | optional
        unknown = sorted(set(self.selector) - allowed)
        if unknown:
            permitted = ", ".join(sorted(allowed)) or "none (this kind takes no selector)"
            raise ValueError(
                f"{self.kind.value} expectation: unknown selector key(s) "
                f"{', '.join(unknown)}. Allowed: {permitted}"
            )
        missing = sorted(required - set(self.selector))
        if missing:
            raise ValueError(
                f"{self.kind.value} expectation needs selector key(s) {', '.join(missing)}"
            )
        if self.kind is ArtifactKind.COMMENT:
            if not self.marker:
                # An empty marker identifies no comment, so `satisfied_by` never matches one:
                # left through, it would be a permanent `failed` on a run that delivered.
                raise ValueError(
                    f"comment expectation: {SELECTOR_MARKER} must be a non-empty substring. An "
                    "empty one matches no comment, so it can never be satisfied"
                )
            if self.where not in (WHERE_ISSUE, WHERE_PR):
                raise ValueError(
                    f"comment expectation: {SELECTOR_WHERE}={self.where!r} is neither "
                    f"{WHERE_ISSUE!r} nor {WHERE_PR!r}"
                )
            if self.where == WHERE_ISSUE and SELECTOR_HEAD_GLOB in self.selector:
                # An issue comment has no head ref, and guessing which half the author meant is
                # how config drifts from behaviour.
                raise ValueError(
                    f"comment expectation: {SELECTOR_HEAD_GLOB} names which pull request carries "
                    f"the comment, so it is meaningful only with {SELECTOR_WHERE}={WHERE_PR!r}"
                )
        return self

    @property
    def marker(self) -> str:
        """The substring a COMMENT must contain. Empty for every other kind."""
        return self.selector.get(SELECTOR_MARKER, "")

    @property
    def where(self) -> str:
        """`issue` (the run's ticket) or `pr`. Meaningful for COMMENT only."""
        return self.selector.get(SELECTOR_WHERE, WHERE_ISSUE)

    def head_glob(self, fallback: str) -> str:
        """The declared glob, or `fallback` when none was declared.

        `fallback` is the branch the run was told to publish: the adapter holds it, this module
        does not, and an expectation that declares no glob means exactly "whatever that was".
        """
        return self.selector.get(SELECTOR_HEAD_GLOB) or fallback

    def satisfied_by(self, artifact: Artifact, *, fallback_glob: str = "") -> bool:
        """Does this reported artifact answer the expectation?

        Matching lives here, not in each adapter, so two cannot read `head_glob` differently:

        - `BRANCH` — the locator's `branch`, globbed.
        - `PR` — the locator's `head`; a PR number alone says nothing about which branch it heads,
          so a probe must report the head ref.
        - `COMMENT` — the locator's `marker`, recorded by the probe that found the comment.
        """
        if artifact.kind is not self.kind:
            return False
        match self.kind:
            case ArtifactKind.BRANCH:
                candidate = artifact.locator.get("branch", "")
            case ArtifactKind.PR:
                candidate = artifact.locator.get("head", "")
            case ArtifactKind.COMMENT:
                return bool(self.marker) and artifact.locator.get(SELECTOR_MARKER) == self.marker
            case _:
                return False
        glob = self.head_glob(fallback_glob)
        return bool(candidate) and bool(glob) and fnmatch.fnmatch(candidate, glob)

    @property
    def summary(self) -> str:
        """One line naming this expectation — GitHub-shaped wording (config still uses `pr`)."""
        return self.display_summary("github")

    def display_summary(self, forge: str) -> str:
        """One line naming this expectation for failure reasons.

        Selectors stay GitHub-shaped in config (`where="pr"`); GitLab-facing messages say `mr`.
        """
        kind = self.kind.value
        if forge == "gitlab" and kind == ArtifactKind.PR.value:
            kind = "mr"
        parts: list[str] = []
        for key, value in sorted(self.selector.items()):
            shown = value
            if forge == "gitlab" and key == SELECTOR_WHERE and value == WHERE_PR:
                shown = "mr"
            parts.append(f'{key}="{shown}"')
        rendered = ", ".join(parts)
        return f"{kind}({rendered})"


def missing_expectations(
    expected: Sequence[ExpectedArtifact],
    artifacts: Sequence[Artifact],
    *,
    fallback_glob: str = "",
) -> tuple[ExpectedArtifact, ...]:
    """Every expectation no reported artifact answers, in declaration order.

    What "unmet" means is one rule for every substrate; what an adapter does about it is its own.
    """
    return tuple(
        expectation
        for expectation in expected
        if not any(
            expectation.satisfied_by(artifact, fallback_glob=fallback_glob)
            for artifact in artifacts
        )
    )


def unmet_expectations(
    expected: Sequence[ExpectedArtifact],
    artifacts: Sequence[Artifact],
    *,
    fallback_glob: str = "",
    forge: str = "github",
) -> tuple[str, ...]:
    """The `display_summary` of every `missing_expectations` entry."""
    return tuple(
        expectation.display_summary(forge)
        for expectation in missing_expectations(expected, artifacts, fallback_glob=fallback_glob)
    )


#: The size a shown file may have and still be served (tech-stack decision 58). The API frames
#: nothing larger.
PAGE_MAX_BYTES: Final = 1_048_576

#: The most files one `preview.files` pattern shows: the run is told it, and the API keeps to it.
PATTERN_MAX_MATCHES: Final = 50


class ShownFile(_Frozen):
    """A file the dashboard shows from the branch the run pushes (`preview.files`, decision 71).
    `path` may hold `{slug}`, as `Workspace.branch` does; its extension decides how the API serves
    it. A `path` with `*` or `**` is a pattern, and `label` then names the set it matches."""

    path: str = Field(min_length=1)
    label: str = Field(min_length=1)


class SecretRoute(_Frozen):
    """Where a substrate that keeps credentials out of its workspace sends one secret: as the
    whole value of `header` on requests to `host`. The operator sets both beside the value, and
    `host` must be on the project's egress allowlist. Not a credential, so it serializes."""

    name: str = Field(min_length=1)
    host: str = Field(min_length=1)
    header: str = Field(min_length=1)


#: rework: the checkout moves to its pull request's head. retry: it stays where the earlier
#: attempt left it, local work included.
ResumeMode = Literal["rework", "retry"]


class ResumeFrom(_Frozen):
    """The previous attempt's session that a rework continues (decision 56)."""

    #: That attempt's run id: its kept workspace is where the session lives.
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    mode: ResumeMode = "rework"


class RunSpec(_Frozen):
    """Everything a substrate needs to start one agent run.

    Secrets are values, not references: minted per run, scoped to the target repo (design.md
    §7.1), injected at spawn, and redacted unconditionally on serialization, so only
    `resolved_secrets()` unwraps them.

    Prompts, skills, and inputs are untrusted repo and ticket text (design.md §7's
    prompt-injection surface) but are not credentials, so they serialize in the clear: an event or
    error payload that hid them would make an injected run unauditable, and containment is
    structural (§7.2, §7.3), never a matter of what the prompt says.
    """

    agent: str = Field(min_length=1, description="manifest folder name == process.yaml `agent:`")
    ticket: str = Field(min_length=1, description="external ticket key, for correlation only")
    workspace: Workspace
    #: Fetched by the orchestrator at the agent's pinned sha — the substrate is handed text, not
    #: told to go read files, so what ran is exactly what was pinned.
    prompts: AgentPrompts
    skills: tuple[AgentSkill, ...] = ()
    #: MCP servers this run may call, already checked against the project's operator allowlist
    #: (design.md §7.5). An adapter whose `supports_mcp` is False must refuse a non-empty tuple
    #: rather than drop it — see that ClassVar.
    mcp: tuple[McpServerSpec, ...] = ()
    #: What this run must deliver (`ExpectedArtifact`). Empty means nothing was declared and each
    #: adapter keeps its own default probe. A declared expectation an adapter cannot verify must
    #: be refused at spawn: dropping it reports a run delivered on evidence nobody looked for.
    expected_artifacts: tuple[ExpectedArtifact, ...] = ()
    inputs: tuple[RunInput, ...] = ()
    #: The images `inputs` embed, fetched by the orchestrator (design.md §7.11). An adapter whose
    #: `supports_attachments` is False must refuse a non-empty tuple rather than drop it.
    attachments: tuple[RunAttachment, ...] = ()
    #: The images `inputs` embed that this run does not get, so the prompt can name them.
    missing_attachments: tuple[MissingAttachment, ...] = ()
    secrets: Mapping[str, SecretStr] = Field(default_factory=dict)
    #: The project secrets a substrate whose egress proxy sets headers (decision 25) delivers
    #: there, the agent seeing a placeholder under each name. A host adapter already puts every
    #: value in the agent's environment and ignores these.
    secret_routes: tuple[SecretRoute, ...] = ()
    #: The project secrets such a substrate puts in the agent's environment anyway, because the
    #: operator chose to let the agent read them. Names only; the values are in `secrets`.
    secret_env: tuple[str, ...] = ()
    #: Concrete model id. Size aliases are an adapter-level error, not a contract concern.
    model: str = Field(min_length=1)
    max_turns: int | None = Field(default=None, gt=0)
    #: Opaque copy of the manifest's `runtime:` block — the only vendor-specific payload, read
    #: by the matching adapter and by nothing else.
    runtime_options: Mapping[str, Any] = Field(default_factory=dict)
    #: Hosts the operator permits this run to reach beyond the substrate's own base list — the
    #: project's `settings.egress_allowlist`, never the repo. Honoured by a substrate that
    #: filters egress (decision 25); a host adapter has no such control and ignores it.
    egress: tuple[str, ...] = ()
    #: Serve the workspace's ports publicly, so a preview (`PreviewHost`) can serve it once the
    #: agent stops: a substrate that sets inbound access only at creation must decide it here
    #: (decision 54). An adapter that is not a `PreviewHost` refuses it (CONFIG).
    public: bool = False
    #: The ports a preview's services declare, for a `public` run: a substrate that filters
    #: inbound traffic admits only these and its own control port (design.md §7.10).
    public_ports: tuple[int, ...] = ()
    #: Continue this session in its kept workspace instead of starting a new conversation. A
    #: substrate that can no longer continue it raises `SessionUnavailable`, and never starts
    #: fresh on its own.
    resume_from: ResumeFrom | None = None
    #: The files the dashboard shows from the branch the run pushes. An adapter that cannot tell
    #: its agent so must refuse a spec that sets them rather than drop them; every shipped adapter
    #: renders them into the first turn.
    shown_files: tuple[ShownFile, ...] = ()

    @field_validator("skills")
    @classmethod
    def _unique_skill_names(cls, value: tuple[AgentSkill, ...]) -> tuple[AgentSkill, ...]:
        # Two SKILL.md files claiming one name in the agent's workspace (a local and a shared skill
        # colliding): silently keeping the last would change agent behaviour invisibly.
        seen: set[str] = set()
        for skill in value:
            if skill.name in seen:
                raise ValueError(f"duplicate skill name {skill.name!r}")
            seen.add(skill.name)
        return value

    @field_validator("mcp")
    @classmethod
    def _unique_server_names(cls, value: tuple[McpServerSpec, ...]) -> tuple[McpServerSpec, ...]:
        # A server name is a tool prefix (`mcp__<server>__<tool>`), so two definitions under one
        # name means the agent's tools depend on which the substrate happened to keep.
        seen: set[str] = set()
        for server in value:
            if server.name in seen:
                raise ValueError(f"duplicate mcp server name {server.name!r}")
            seen.add(server.name)
        return value

    @field_validator("secret_routes")
    @classmethod
    def _one_header_per_host(cls, value: tuple[SecretRoute, ...]) -> tuple[SecretRoute, ...]:
        # A proxy rule sets one value per header on a host: a second would silently win.
        seen: set[tuple[str, str]] = set()
        for route in value:
            key = (route.host.lower(), route.header.lower())
            if key in seen:
                raise ValueError(f"two secrets set header {route.header!r} on {route.host!r}")
            seen.add(key)
        return value

    @field_validator("attachments")
    @classmethod
    def _unique_attachment_names(
        cls, value: tuple[RunAttachment, ...]
    ) -> tuple[RunAttachment, ...]:
        # A name is a file stem: two under one name would leave one image overwriting the other.
        seen: set[str] = set()
        for item in value:
            if item.name in seen:
                raise ValueError(f"duplicate attachment name {item.name!r}")
            seen.add(item.name)
        return value

    @model_validator(mode="after")
    def _ports_are_public(self) -> RunSpec:
        if self.public_ports and not self.public:
            raise ValueError("public_ports needs public")
        if CONTROL_PLANE_PORT in self.public_ports:
            raise ValueError(_CONTROL_PLANE_RESERVED)
        return self

    @field_serializer("secrets", when_used="always")
    def _redact(self, secrets: Mapping[str, SecretStr]) -> dict[str, str]:
        # A RunSpec reaches logs, events, and error payloads: `model_dump()` must never carry a
        # live credential (design.md §7.1).
        return dict.fromkeys(secrets, REDACTED)

    def resolved_secrets(self) -> dict[str, str]:
        """Unwrap secret values. The single sanctioned call site is an adapter's injection
        strategy — grep for it before adding a second."""
        return {key: value.get_secret_value() for key, value in self.secrets.items()}


class RunHandle(_Frozen):
    """What `spawn` returns. `run_id` is the substrate's own id and is opaque above the
    adapter — the orchestrator keys its own state on `(ticket, step, attempt)`."""

    run_id: str = Field(min_length=1)
    status: RunStatus = RunStatus.QUEUED


class RunResult(_Frozen):
    """Terminal report for a run. Cost and token counts are recorded per run by the
    orchestrator; the runtime only reports what the substrate measured."""

    run_id: str = Field(min_length=1)
    outcome: RunStatus
    summary: str = ""
    artifacts: tuple[Artifact, ...] = ()
    cost_usd: Decimal | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    #: The cache split of `input_tokens`. The local adapters report `input_tokens` GROSS of
    #: it — codex natively, claude_cli by summing the CLI's fresh, cache-written and cache-read
    #: parts — so pricing subtracts it; Claude only differs in pricing the run itself.
    #: `cache_write_input_tokens` exists because a cache write is billed at a premium (Claude,
    #: some OpenAI models); None on any adapter whose substrate reports no such split.
    cached_input_tokens: int | None = Field(default=None, ge=0)
    cache_write_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    failure_reason: str | None = None
    #: Facts the adapter reads off the stream that the run ended without, so a CLI version that
    #: renamed one shows up rather than reading as a zero. Never changes `outcome`.
    stream_warnings: tuple[str, ...] = ()
    #: The conversation the run had, so a rework can continue it (`ResumeFrom`). None when the
    #: CLI never announced one.
    session_id: str | None = None
    #: Why an `INTERRUPTED` run parked; None for any other outcome.
    park_reason: ParkReason | None = None

    @field_validator("outcome")
    @classmethod
    def _terminal_only(cls, value: RunStatus) -> RunStatus:
        if not value.is_terminal:
            raise ValueError(f"a result exists only for a terminal run, got {value}")
        return value


class PreviewPhase(StrEnum):
    """What a kept run's preview is doing (decision 54). Never a run status: a preview is
    informational, and nothing about it changes whether the run succeeded."""

    #: Nothing serves: never started, parked, or released.
    STOPPED = "stopped"
    #: Setup or a service is still starting.
    STARTING = "starting"
    #: Every service answered its ready check.
    READY = "ready"
    #: Setup failed, a service never answered, or one stopped answering.
    FAILED = "failed"


#: The `detail` of a STOPPED `PreviewState` the substrate stopped for one of these reasons:
#: nobody kept it past its `keep_until`; its sandbox paused for its session cap, after which it
#: can start again; or a rework took its workspace over and carries it (design.md §3.2).
PREVIEW_IDLE: Final = "idle"
PREVIEW_RENEWAL: Final = "renewal"
PREVIEW_CONTINUED: Final = "continued"

#: The port E2B's control plane (envd) reaches every sandbox on. A sandbox's firewall always admits
#: it, and a contain load cuts every connection on a declared port, so no preview declares it.
CONTROL_PLANE_PORT: Final = 49983
_CONTROL_PLANE_RESERVED: Final = (
    f"port {CONTROL_PLANE_PORT} is reserved for the sandbox's control plane"
)


class PreviewServiceSpec(_Frozen):
    """One process a kept workspace serves: `command` as the preview's own user, with `PORT`
    set."""

    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    command: str = Field(min_length=1)
    port: int = Field(ge=1024, le=65535)
    ready_path: str = "/"
    ready_timeout: timedelta = timedelta(seconds=120)

    @field_validator("port")
    @classmethod
    def _not_the_control_plane(cls, value: int) -> int:
        if value == CONTROL_PLANE_PORT:
            raise ValueError(_CONTROL_PLANE_RESERVED)
        return value


class PreviewSpec(_Frozen):
    """A preview as the substrate runs it: `setup` once per workspace, then every service, closed
    for idle `stop_after` from its start until `preview_status` is given a `keep_until`."""

    setup: tuple[str, ...] = ()
    services: tuple[PreviewServiceSpec, ...] = Field(min_length=1)
    stop_after: timedelta = timedelta(minutes=30)


class PreviewUrl(_Frozen):
    service: str
    url: str


class PreviewState(_Frozen):
    phase: PreviewPhase
    #: The `token` the current preview was started with; None when none ever was.
    token: str | None = None
    #: One per service, in declaration order, only while READY.
    urls: tuple[PreviewUrl, ...] = ()
    #: Why it FAILED or STOPPED. Read from a machine the agent controlled: data, never executed.
    detail: str | None = None
    #: What the checkout held when it became READY. Advisory, for the same reason.
    head_sha: str | None = None
    uncommitted: bool = False


class RunUsage(_Frozen):
    """What a live session has spent so far, counted off its own log (decision 56). The log is
    the agent's to write, so this is an estimate: the end's `RunResult` replaces it."""

    #: Gross of the cache split, as `RunResult.input_tokens` is.
    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cache_write_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class SessionState(_Frozen):
    """Where a RUNNING or IDLE session stands, as the substrate observed it.

    No mint time: an adapter never mints (design.md §7.1), so the orchestrator reads that from
    its own events.
    """

    #: Both None while RUNNING, both set while IDLE.
    idle_reason: IdleReason | None = None
    idle_since: AwareDatetime | None = None
    #: What the agent asked, only when `idle_reason` is QUESTION. Agent-written: data, never
    #: executed or interpolated.
    question: str | None = None
    #: When the CLI answers `question` itself if nobody has, only while idle on a question.
    question_expires_at: AwareDatetime | None = None
    #: Why the last turn failed, only when `idle_reason` is TURN_ERROR. CLI-written from the
    #: session log, which the agent can reach: data, as `question` is.
    turn_error: str | None = None
    turns: int = Field(default=0, ge=0)
    #: The substrate's own cap on this session (E2B's); None where there is none.
    session_deadline: AwareDatetime | None = None
    #: Where this attempt's slice of the session log starts: one log can span a rework.
    log_offset: int = Field(default=0, ge=0)
    #: When a writer last typed into the terminal.
    last_input_at: AwareDatetime | None = None
    session_id: str | None = None
    #: The run's tokens so far; None before its log carried any.
    usage: RunUsage | None = None

    @model_validator(mode="after")
    def _idle_is_whole(self) -> SessionState:
        if (self.idle_reason is None) != (self.idle_since is None):
            raise ValueError("idle_reason and idle_since are set together or not at all")
        if self.question is not None and self.idle_reason is not IdleReason.QUESTION:
            raise ValueError("a question is carried only while idle on a question")
        if self.question_expires_at is not None and self.idle_reason is not IdleReason.QUESTION:
            raise ValueError("a question's expiry is carried only while idle on a question")
        if self.turn_error is not None and self.idle_reason is not IdleReason.TURN_ERROR:
            raise ValueError("a turn error is carried only while idle on a failed turn")
        return self


class TerminalTarget(_Frozen):
    """What the terminal gateway attaches to (decision 57). Opaque to the orchestrator, which
    only passes it on."""

    substrate: str = Field(min_length=1)
    #: Both reach a tmux command line, on E2B as root, so nothing a shell would read twice.
    socket: str = Field(pattern=r"^[A-Za-z0-9_./-]+$")
    session: str = Field(pattern=r"^[A-Za-z0-9_./-]+$")
    sandbox_id: str | None = None


class InspectPhase(StrEnum):
    """What a kept run's inspect shell is doing (decision 57). Like a preview, never a run
    status."""

    STARTING = "starting"
    #: The shell runs, and `InspectState.target` is where the gateway attaches.
    OPEN = "open"
    #: No shell: never started, exited, parked, or its workspace released.
    STOPPED = "stopped"
    FAILED = "failed"


class InspectState(_Frozen):
    phase: InspectPhase
    #: The `token` the current shell was started with; None when none ever was.
    token: str | None = None
    target: TerminalTarget | None = None
    #: Terminal clients attached now: the idle park waits for none.
    clients: int = Field(default=0, ge=0)
    last_activity: AwareDatetime | None = None
    started_at: AwareDatetime | None = None
    #: Why it FAILED or STOPPED. Read from a machine the agent controlled: data, never executed.
    detail: str | None = None

    @model_validator(mode="after")
    def _target_only_while_open(self) -> InspectState:
        if (self.target is not None) != (self.phase is InspectPhase.OPEN):
            raise ValueError("a target is carried exactly while the shell is open")
        return self


# --------------------------------------------------------------------------- protocol


@runtime_checkable
class RuntimeAdapter(Protocol):
    """The operations of design.md §3.2, plus the capabilities the orchestrator must ask about
    rather than assume. Every capability is a ClassVar: it cannot vary per run."""

    #: Whether the substrate can call back, or `status()` polling is the only path to terminal
    #: state (design.md §3.2 makes callbacks optional per adapter).
    supports_callbacks: ClassVar[bool]

    #: Whether this adapter can wire `RunSpec.mcp` into the substrate. False means `spawn` must
    #: raise `SpawnError` for a spec that declares servers: an agent running without the tool its
    #: prompt assumes fails as if it were a bad agent, not as missing config.
    supports_mcp: ClassVar[bool]

    #: Whether this adapter can honour `Workspace(isolation="blind")`. False means `spawn` must
    #: raise `SpawnError` for a blind request rather than run it on a shared checkout — dropping a
    #: containment requirement silently is worse than refusing to start. Every shipped adapter is
    #: False.
    supports_blind_workspace: ClassVar[bool]

    #: Whether this adapter can hand `RunSpec.attachments` to the agent: as files outside the
    #: checkout, named in the prompt, and attached to the first turn. False means `spawn` and
    #: `resume` must raise `SpawnError` (CONFIG) for a non-empty tuple — an agent told about an
    #: image it never got fails as if it were a bad agent.
    supports_attachments: ClassVar[bool]

    #: How many runs this adapter may have in flight at once, or `None` for no adapter-level cap.
    max_concurrent_runs: ClassVar[int | None]

    #: The vendor CLI/API version this adapter was verified against (its PINNED CLI/API SURFACE
    #: block), which a run records as the CLI it booted.
    pinned_version: ClassVar[str]

    async def spawn(self, run_spec: RunSpec) -> RunHandle:
        """Start a run. Raises `SpawnError` (nothing started) or `AdapterUnavailable`."""
        ...

    async def status(self, run_id: str) -> RunStatus:
        """Current state. Raises `UnknownRunError` if the substrate has no such run."""
        ...

    async def result(self, run_id: str) -> RunResult:
        """Terminal report. Only meaningful once `status()` is terminal."""
        ...

    def logs(self, run_id: str) -> AsyncIterator[str]:
        """Stream log lines. Declared non-async so an implementation can be an async generator.
        Not a file path: that would be a CLI-shaped assumption."""
        ...

    async def cancel(self, run_id: str) -> None:
        """Request cancellation. Idempotent; observing `cancelled` is still done via
        `status()`."""
        ...

    async def interrupt(self, run_id: str, reason: ParkReason = ParkReason.USER) -> None:
        """Stop the run but KEEP its workspace, so the session can be resumed (decision 26).

        `reason` says why it parks: a human's stop, an idle session, or a rollover
        onto a fresh token.

        Not `cancel` with a flag. `cancel` finalises and cleans up, and its outcome is one the
        loop retries — all three are right for a deadline and wrong for a human who stopped a run
        to redirect it. The workspace this preserves is the thing a resume runs in, so a caller
        that wanted `cancel` would be destroying exactly what it needs.

        Idempotent, and terminal: the process really does stop. What differs from `cancel` is
        only what survives it and what the orchestrator does next. Every adapter keeps the
        workspace; one that cannot does not meet this contract.
        """
        ...

    async def resume(
        self,
        run_id: str,
        turn: str | None,
        secrets: Mapping[str, SecretStr],
        attachments: tuple[RunAttachment, ...] = (),
        missing_attachments: tuple[MissingAttachment, ...] = (),
    ) -> RunHandle:
        """Continue an interrupted run's conversation with `turn` as its next input.

        The run is `INTERRUPTED` because `interrupt` parked it, or because its session ended with
        nobody asking (`ParkReason.EXITED`).

        A None `turn` brings the session back at its prompt with nothing typed.

        The SAME run, not a new one: the run id is unchanged, the workspace is the one the
        interrupt preserved, and the agent keeps the context it had. That is the difference from
        spawning again, where the agent starts over from the spec and pays for it.

        `secrets` is passed in for the same reason `spawn` takes a `RunSpec`: the orchestrator owns
        credentials, and an adapter that reads its own process environment makes the secret store
        authoritative in name only. It excludes `GH_TOKEN` unless the caller minted a fresh one —
        an adapter never mints (design.md §7.1).

        Raises `UnknownRunError` when the substrate can no longer address the session — the
        workspace was cleaned up, the conversation expired, or the run never got far enough to
        have one. The caller must treat that as "cannot continue" and say so, never as licence to
        start a fresh run: a human who asked to resume did not ask to pay for a restart that may
        diverge from what the agent already did.

        `attachments` are the images the steering comments embed, delivered with `turn` the way a
        spawn delivers `RunSpec.attachments` with the first turn; `missing_attachments` are the
        ones it does not get, for the turn to name.
        """
        ...

    async def finish(self, run_id: str) -> None:
        """End an IDLE session gracefully: when its postconditions hold, or when it stopped with
        nothing asked and nobody engaged, after which the worker fails the run on them. The run
        then reaches a terminal state, and `result` reads it as any other. On an INTERRUPTED run
        accepted without a resume, ends its park: its outcome and result stay, and it keeps no
        more than any other end keeps. Idempotent."""
        ...

    async def session_state(self, run_id: str) -> SessionState:
        """Where a RUNNING or IDLE session stands. Raises only `AgentRuntimeError`s,
        even for a run that has already ended: `UnknownRunError` if the substrate has no such
        run."""
        ...

    async def terminal_target(self, run_id: str) -> TerminalTarget:
        """Where the terminal gateway attaches to this run's session (decision 57)."""
        ...

    async def spawn_refusal(self) -> str | None:
        """Why the substrate as configured now must not run a session, or None when it may: the
        session-log translator is proven only against the CLI it was checked on. Asked before every
        spawn, never by `spawn` itself, which a smoke uses to give that proof. Raises an
        `AgentRuntimeError` when the substrate cannot say."""
        ...


@runtime_checkable
class CredentialSweeper(Protocol):
    """Optional: a substrate that keeps runs' credential files on the worker's own disk."""

    def sweep_credentials(self) -> int:
        """Delete the credential files of every terminal run whose process is gone, and return
        how many went. Called once at worker startup, before any tick, when no resume can be
        writing them."""
        ...


@runtime_checkable
class WorkspaceReaper(Protocol):
    """Optional: a substrate whose workspaces outlive the process that made them.

    A paused sandbox or a host worktree is kept until something deletes it, and the adapter
    cannot see ticket state, so the orchestrator says which runs are still wanted and the
    substrate removes the rest.
    """

    async def reap(self, keep: Collection[str], *, retain: timedelta | None) -> tuple[str, ...]:
        """Remove the workspace of every run this deployment made that `keep` does not name,
        and, where the substrate keeps a separate local record of its runs, every terminal
        run's record older than `retain` (none when `retain` is None: several reapers can share
        one record store, which one of them prunes). Returns the run ids whose workspace was
        removed. Idempotent."""
        ...


@runtime_checkable
class PreviewHost(Protocol):
    """Optional: a substrate that can serve a run's workspace to a browser (decision 54), beside
    its live session or an open session as well as once it has ended. Only a run spawned
    `public` can be previewed. No verb here changes the run's status: a preview is informational.
    """

    async def preview_start(
        self, run_id: str, spec: PreviewSpec, env: Mapping[str, SecretStr], *, token: str
    ) -> PreviewState:
        """Wake the workspace if it sleeps, run `setup` once, and start the services. Returns at
        once, in STARTING; `preview_status` reports the rest. Idempotent on `token`: the same
        token while STARTING or READY starts nothing; a new one while it serves restarts the
        services in place. Raises `PreviewUnavailable`."""
        ...

    async def preview_status(
        self, run_id: str, *, keep_until: datetime | None = None
    ) -> PreviewState:
        """What the preview is doing now. Advances STARTING to READY or FAILED. `keep_until`,
        when given, is when it closes for idle (`PREVIEW_IDLE`), and the workspace is kept awake
        until then; None keeps the last one given. A substrate with a session cap stops it at the
        cap first (`PREVIEW_RENEWAL`), after which it can start again. Never wakes a parked
        workspace."""
        ...

    async def preview_stop(self, run_id: str) -> None:
        """Stop the services and leave the workspace to a live run or an open view, or park it
        without a credential when nothing else runs in it. Idempotent."""
        ...


@runtime_checkable
class InspectHost(Protocol):
    """Optional: a substrate that can open a finished session's own CLI read-only in its kept
    workspace, with the model credential only (decision 57). No verb here changes the run's
    status."""

    async def inspect_start(
        self, run_id: str, *, token: str, limit: timedelta, credentials: Mapping[str, SecretStr]
    ) -> InspectState:
        """Open the run's own session read-only, ended again `limit` from its start: wake the
        workspace and relaunch its CLI on its session there, with `credentials` (the model
        credential only, never `GH_TOKEN`) and no MCP server. Idempotent
        on `token` once OPEN: the same token starts nothing again. Raises
        `InspectUnavailable`."""
        ...

    async def inspect_status(self, run_id: str) -> InspectState:
        """What the view is doing now, ending one past its limit. Never wakes a workspace."""
        ...

    async def inspect_stop(self, run_id: str) -> None:
        """End the view, and leave no credential its launch wrote. Idempotent."""
        ...
