"""SQLAlchemy models — the state machine's storage (data-model.md).

Enumerations are TEXT + CHECK, never a native Postgres enum: adding a value is a CHECK swap
rather than an `ALTER TYPE` that cannot run in a transaction. The StrEnums below are the
application-side spelling of those CHECKs; keep the two in step.

Relationships are `lazy="raise"` throughout so a forgotten `selectinload()` fails loudly
instead of emitting IO from an async context that cannot service it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ColumnElement,
    Date,
    Double,
    ForeignKey,
    Index,
    Numeric,
    SQLColumnExpression,
    UniqueConstraint,
    and_,
    func,
    select,
    text,
)
from sqlalchemy.orm import Mapped, MappedColumn, column_property, mapped_column, relationship

from orchestrator.db.base import Base, CreatedAt, IdPk, JsonDict, UpdatedAt

__all__ = [
    "ARTIFACT_DIRECTIONS",
    "ARTIFACT_KINDS",
    "CHAT_ITEM_KINDS",
    "CHAT_PROPOSAL_STATUSES",
    "CHAT_TURN_LIVE",
    "CHAT_TURN_STATUSES",
    "CHAT_TURN_TRIGGERS",
    "ENTERED_BY",
    "MCP_ACCESSES",
    "MCP_MINTED_VIAS",
    "REMOVAL_REASONS",
    "RUNTIME_KINDS",
    "RUNTIME_TYPES",
    "STEP_KINDS",
    "STEP_RUN_STATUSES",
    "TICKET_SOURCE_KINDS",
    "TICKET_STATUSES",
    "VERIFICATION_PURPOSES",
    "Agent",
    "AppSetting",
    "AppUser",
    "Artifact",
    "ArtifactDirection",
    "ArtifactKind",
    "ChatBackendState",
    "ChatConversation",
    "ChatItem",
    "ChatItemKind",
    "ChatProposal",
    "ChatProposalStatus",
    "ChatSpend",
    "ChatTurn",
    "ChatTurnStatus",
    "ChatTurnTrigger",
    "EmailVerification",
    "EnteredBy",
    "Event",
    "McpAccess",
    "McpMintedVia",
    "McpToken",
    "Organization",
    "OrganizationCredential",
    "OrganizationInvite",
    "OrganizationMember",
    "OrganizationRemoval",
    "OrganizationSetting",
    "PermissionRecord",
    "ProcessVersion",
    "Project",
    "RemovalReason",
    "Role",
    "RolePermission",
    "Runtime",
    "RuntimeCredential",
    "RuntimeKind",
    "RuntimeType",
    "Secret",
    "StepKind",
    "StepRun",
    "StepRunStatus",
    "Ticket",
    "TicketSource",
    "TicketSourceKind",
    "TicketStatus",
    "VerificationPurpose",
    "intake_column",
    "intake_repo",
]


class TicketSourceKind(StrEnum):
    GITHUB = "github"
    JIRA = "jira"
    GITLAB = "gitlab"


class RuntimeKind(StrEnum):
    CLAUDE_MANAGED = "claude_managed"
    CODEX = "codex"
    #: The Claude family's own name (revision 0012). `claude_managed` is the deferred adapter's
    #: name and stays accepted for every pinned snapshot; a new manifest writes this one.
    CLAUDE = "claude"


class RuntimeType(StrEnum):
    """What a product-level `runtime` row IS: a vendor family and how it authenticates.

    Not a `RuntimeKind`: that is what a manifest declares (`codex`, `claude`) or what the
    adapter registry executes (`codex_cli`, `claude_cli`). `factorial.runtimes` maps a type
    onto both, so one vocabulary can never be mistaken for the other.
    """

    CODEX_API = "codex_api"
    CODEX_SUBSCRIPTION = "codex_subscription"
    #: Codex CLI against GovCloud Bedrock. The factory host role mints the bearer; the row
    #: holds no vendor key.
    CODEX_BEDROCK = "codex_bedrock"
    CLAUDE_API = "claude_api"
    CLAUDE_SUBSCRIPTION = "claude_subscription"


class TicketStatus(StrEnum):
    RUNNING = "running"
    WAITING_GATE = "waiting_gate"
    BLOCKED = "blocked"
    DONE = "done"
    #: Off the factory floor and able to come back (decision 29). Leased and observed like
    #: BLOCKED — not DONE, which is never leased again — because a human removing `factorial:exit`
    #: on GitHub is one of the two ways back.
    EXITED = "exited"


class StepKind(StrEnum):
    AGENT = "agent"
    HUMAN_GATE = "human_gate"
    #: `kind: action` (design.md §4) — dispatched to `orchestrator.actions`, never a manifest, so
    #: `StepRun.runtime_kind` stays NULL for these rows.
    ACTION = "action"
    #: `kind: check` — completes on observed conditions alone; no substrate, so
    #: `StepRun.runtime_kind` stays NULL for these rows too.
    CHECK = "check"


class StepRunStatus(StrEnum):
    RUNNING = "running"
    #: waiting / approved / rejected are the human-gate states.
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    APPROVED = "approved"
    REJECTED = "rejected"
    #: A human stopped this run to redirect it. Deliberately NOT in
    #: `worker.tick_context._FAILED_STATUSES` (decision 26): a failure is retried, an interrupt
    #: parks its ticket until a human releases it. The run's workspace is kept so the session can be
    #: resumed rather than redone.
    INTERRUPTED = "interrupted"
    #: A session alive at its prompt (decision 56): live like RUNNING, but the worker
    #: decides whether its session is finished or waits for a human.
    IDLE = "idle"


class CostSource(StrEnum):
    """Where `StepRun.cost_usd` came from. NULL is a run nothing could price."""

    #: The substrate measured it (Claude's CLI reports `total_cost_usd`).
    REPORTED = "reported"
    #: The worker priced the token split at list (`worker.pricing`); notional on a subscription.
    ESTIMATED = "estimated"


class EnteredBy(StrEnum):
    """Why a `step_run` attempt exists (revision 0023): the `step_entered.reason` of the entry that
    predicted its attempt number (`worker.tick_context.EnterReason`), or `retry` when no entry did —
    a failure retry (`worker.failures`) and a human-granted attempt (`worker.reconcile`) re-run a
    step without re-entering it. Revision 0023 backfills `retry` only for rows created at or after
    their ticket's first entry, and leaves older unnamed rows NULL."""

    NEXT = "next"
    REJECT_RERUN = "reject_rerun"
    ADMISSION = "admission"
    RETRY = "retry"
    #: The first run of a generation a restart began (tech-stack.md decision 67).
    RESTART = "restart"
    #: The first run of a step a person redid (decision 76).
    REDO = "redo"


class ArtifactDirection(StrEnum):
    INPUT = "input"
    OUTPUT = "output"


class ArtifactKind(StrEnum):
    BRANCH = "branch"
    PR = "pr"
    COMMIT = "commit"
    FILE = "file"
    COMMENT = "comment"


class McpAccess(StrEnum):
    READ = "read"
    WRITE = "write"
    #: One assistant turn's token: reads and proposes, never writes (design.md §7.16).
    CHAT = "chat"


class McpMintedVia(StrEnum):
    SESSION = "session"
    API_TOKEN = "api_token"
    CHAT = "chat"


class RemovalReason(StrEnum):
    REMOVED = "removed"
    LEFT = "left"


class VerificationPurpose(StrEnum):
    VERIFY = "verify"
    RESET = "reset"


class ChatTurnStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: The process died under the turn; the next claim fails it rather than resuming it.
    INTERRUPTED = "interrupted"


CHAT_TURN_LIVE: Final = (ChatTurnStatus.QUEUED, ChatTurnStatus.RUNNING)


class ChatTurnTrigger(StrEnum):
    USER = "user"
    #: The continuation that follows an approved proposal.
    APPROVAL = "approval"


class ChatItemKind(StrEnum):
    USER_TEXT = "user_text"
    ASSISTANT_TEXT = "assistant_text"
    #: One call, written at the call and updated with its result.
    TOOL = "tool"
    PROPOSAL = "proposal"
    SHOW_ME = "show_me"
    CHART = "chart"
    EDITOR_DRAFT = "editor_draft"
    NOTICE = "notice"


class ChatProposalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    SUCCEEDED = "succeeded"
    #: Approved, but the service refused: the world moved since the proposal was made.
    REFUSED = "refused"
    DECLINED = "declined"
    EXPIRED = "expired"


TICKET_SOURCE_KINDS: Final[tuple[str, ...]] = tuple(TicketSourceKind)
RUNTIME_KINDS: Final[tuple[str, ...]] = tuple(RuntimeKind)
RUNTIME_TYPES: Final[tuple[str, ...]] = tuple(RuntimeType)
TICKET_STATUSES: Final[tuple[str, ...]] = tuple(TicketStatus)
STEP_KINDS: Final[tuple[str, ...]] = tuple(StepKind)
STEP_RUN_STATUSES: Final[tuple[str, ...]] = tuple(StepRunStatus)
COST_SOURCES: Final[tuple[str, ...]] = tuple(CostSource)
ENTERED_BY: Final[tuple[str, ...]] = tuple(EnteredBy)
ARTIFACT_DIRECTIONS: Final[tuple[str, ...]] = tuple(ArtifactDirection)
ARTIFACT_KINDS: Final[tuple[str, ...]] = tuple(ArtifactKind)
MCP_ACCESSES: Final[tuple[str, ...]] = tuple(McpAccess)
MCP_MINTED_VIAS: Final[tuple[str, ...]] = tuple(McpMintedVia)
REMOVAL_REASONS: Final[tuple[str, ...]] = tuple(RemovalReason)
VERIFICATION_PURPOSES: Final[tuple[str, ...]] = tuple(VerificationPurpose)
CHAT_TURN_STATUSES: Final[tuple[str, ...]] = tuple(ChatTurnStatus)
CHAT_TURN_TRIGGERS: Final[tuple[str, ...]] = tuple(ChatTurnTrigger)
CHAT_ITEM_KINDS: Final[tuple[str, ...]] = tuple(ChatItemKind)
CHAT_PROPOSAL_STATUSES: Final[tuple[str, ...]] = tuple(ChatProposalStatus)


def _one_of(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(str(v)) for v in values)})"


class Organization(Base):
    """The tenant root (design.md §2.11): what a tenant brings, and what its people act through,
    hangs off one of these. `slug` is the public key, immutable after create like `runtime.slug`;
    `id` is what every foreign key holds. Revision 0027 seeds `default` and puts every existing
    row in it. Not an event source: `event.project_id` is NOT NULL, so `updated_by` says who
    renamed it, as `app_setting` does."""

    __tablename__ = "organization"
    __table_args__ = (UniqueConstraint("slug"),)

    id: Mapped[IdPk]
    slug: Mapped[str]
    name: Mapped[str]
    #: Organization-level knobs, validated by Pydantic on read like `project.settings`; empty
    #: until something needs one.
    settings: Mapped[JsonDict] = mapped_column(server_default=text("'{}'::jsonb"))
    updated_by: Mapped[str]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


def _organization_slug(organization_id: MappedColumn[int]) -> Mapped[str]:
    """The owning organization's slug, loaded with the row: every item answer names its
    organization, so a client can build the org-scoped link without a second request."""
    return column_property(
        select(Organization.slug)
        .where(Organization.id == organization_id)
        .correlate_except(Organization)
        .scalar_subquery()
    )


def _organization_fk(*, index: bool = False) -> MappedColumn[int]:
    """A foreign key to the organization that owns the row outright. RESTRICT, never CASCADE:
    deleting an organization with projects would cascade through every ticket and event."""
    return mapped_column(
        BigInteger, ForeignKey("organization.id", ondelete="RESTRICT"), index=index
    )


class Project(Base):
    """Aggregate root. `github_repo` is the 1:1 artifact + `.factorial/` config repo."""

    __tablename__ = "project"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)

    id: Mapped[IdPk]
    organization_id: Mapped[int] = _organization_fk()
    organization_slug: Mapped[str] = _organization_slug(organization_id)
    name: Mapped[str]
    description: Mapped[str | None]
    github_repo: Mapped[str]
    #: Project-level knobs (agent_runtime, app_runtime); validated by Pydantic on read.
    settings: Mapped[JsonDict] = mapped_column(server_default=text("'{}'::jsonb"))
    #: The admission switch: `worker.admission.admit_all` only considers active projects, so a
    #: half-configured project cannot pull issues into a process. Default false.
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]

    tickets: Mapped[list[Ticket]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )


class TicketSource(Base):
    """What the project reacts to. Separate from `github_repo`: intake and artifacts can differ."""

    __tablename__ = "ticket_source"
    __table_args__ = (
        CheckConstraint(_one_of("kind", TICKET_SOURCE_KINDS), name="kind"),
        UniqueConstraint("project_id"),  # one source per project (relax later)
    )

    id: Mapped[IdPk]
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("project.id", ondelete="CASCADE")
    )
    kind: Mapped[str]
    external_ref: Mapped[str]
    #: Name of the `secret` row holding the webhook secret, not the secret itself.
    webhook_secret_ref: Mapped[str | None]
    last_polled_at: Mapped[datetime | None]
    #: The last GitHub delivery that verified against this project's secret, and the last that
    #: did not. Overwritten in place; the dashboard shows whether the webhook works.
    webhook_verified_at: Mapped[datetime | None]
    webhook_failed_at: Mapped[datetime | None]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


def intake_repo(project: Project, source: TicketSource | None) -> str:
    """The repo admission reads a project's issues from: the ticket source's `external_ref`, or
    `github_repo` when that is empty or the project has no source."""
    return (source.external_ref if source is not None else "") or project.github_repo


def intake_column() -> ColumnElement[str]:
    """`intake_repo` in SQL, over `Project` outer-joined to `TicketSource`."""
    return func.coalesce(func.nullif(TicketSource.external_ref, ""), Project.github_repo)


class ProcessVersion(Base):
    """Append-only snapshot of the parsed ADLC config; a ticket pins one at admission.

    No `updated_at`: a config edit writes a new row, it never rewrites one.
    """

    __tablename__ = "process_version"
    __table_args__ = (
        # Named: the convention's name for four columns is past Postgres's 63 characters.
        UniqueConstraint(
            "project_id",
            "source_repo",
            "process_key",
            "source_commit_sha",
            name="uq_process_version_project_repo_key_sha",
        ),
    )

    id: Mapped[IdPk]
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("project.id", ondelete="CASCADE")
    )
    #: Which lane this snapshot belongs to — the config's own `name:`.
    process_key: Mapped[str]
    content: Mapped[JsonDict]
    #: The repository and commit this snapshot was read from, and its identity: `(project, repo,
    #: lane, sha)` is unique, so re-syncing a sha finds the row it already wrote and a new sha, or
    #: the same sha in a fork, writes a new one. A sha is immutable, which is what lets the sync
    #: compare nothing and still be sure. Every pinned read goes to this pair, never to the
    #: project's current `github_repo`, which may have moved since (revision 0042).
    source_repo: Mapped[str]
    source_commit_sha: Mapped[str]
    #: The ref the sync ASKED for, beside the sha it resolved to; `factorial` when it named none.
    #: NULL = the repo's default branch: rows written before issue #248, when that was the default.
    source_ref: Mapped[str | None]
    #: `{agent name: {"sha", "path"}}` — where each referenced agent's definition was read from
    #: when this snapshot was registered, so a ticket freezes its agents along with its steps.
    #: NULL means the row predates revision 0013; it falls back to `agent.pinned_commit_sha`,
    #: which is what it did before.
    agent_pins: Mapped[JsonDict | None]
    #: When the last sync stopped finding this lane's file in `.factorial/`, or when a sync of
    #: another repository than `source_repo` ran. A timestamp = retired, and reintroducing the file
    #: clears it. NULL is not enough to be offered: `source_repo` must also be the project's
    #: (`factorial.registry.offered_version`). Availability is never consulted for a ticket
    #: already in flight — that ticket holds `process_version_id`.
    retired_at: Mapped[datetime | None]
    #: When a sync last landed on this snapshot. The newest per lane is current, so re-syncing an
    #: older commit makes it current again (revision 0018).
    activated_at: Mapped[CreatedAt]
    created_at: Mapped[CreatedAt]


class Agent(Base):
    """Registry row only; the agent definition itself lives in the repo at `repo_path`."""

    __tablename__ = "agent"
    __table_args__ = (
        CheckConstraint(_one_of("runtime_kind", RUNTIME_KINDS), name="runtime_kind"),
        UniqueConstraint("project_id", "name"),
    )

    id: Mapped[IdPk]
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("project.id", ondelete="CASCADE")
    )
    name: Mapped[str]
    repo_path: Mapped[str]
    pinned_commit_sha: Mapped[str | None]
    #: NULL = inherit project.settings.agent_runtime; a value here is a per-agent override.
    runtime_kind: Mapped[str | None]
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


#: How a run is supplied a project secret (decision 75): its environment, or a header a cloud
#: sandbox's egress proxy sets.
SECRET_DELIVERIES: Final[tuple[str, ...]] = ("header", "env")


class Secret(Base):
    """Ciphertext + key id only; plaintext is never stored, logged, or put in an event."""

    __tablename__ = "secret"
    __table_args__ = (
        UniqueConstraint("project_id", "name"),
        CheckConstraint(_one_of("sandbox_delivery", SECRET_DELIVERIES), name="sandbox_delivery"),
        CheckConstraint(
            "(sandbox_delivery = 'header' AND egress_host IS NOT NULL "
            "AND egress_header IS NOT NULL) OR (sandbox_delivery = 'env' "
            "AND egress_host IS NULL AND egress_header IS NULL)",
            name="egress_route",
        ),
    )

    id: Mapped[IdPk]
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("project.id", ondelete="CASCADE")
    )
    name: Mapped[str]
    value_ciphertext: Mapped[bytes]
    enc_key_id: Mapped[str]
    sandbox_delivery: Mapped[str] = mapped_column(server_default=text("'env'"))
    egress_host: Mapped[str | None]
    egress_header: Mapped[str | None]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class Runtime(Base):
    """An operator-defined substrate a repo's `process.yaml` may only reference by `slug`.

    The same trust direction as `project.settings.mcp_allowlist` (design.md §7.5): the repo
    agents write to selects; this row, which only an operator can write, defines. `slug` and
    `type` are immutable after create — repos pin the first, credentials are shaped by the
    second — and the API enforces that, not the database.
    """

    __tablename__ = "runtime"
    __table_args__ = (
        CheckConstraint(_one_of("type", RUNTIME_TYPES), name="type"),
        UniqueConstraint("organization_id", "slug"),
    )

    id: Mapped[IdPk]
    #: A runtime holds a tenant's vendor credentials, so it belongs to one organization, and a
    #: slug names a row within that organization only.
    organization_id: Mapped[int] = _organization_fk()
    organization_slug: Mapped[str] = _organization_slug(organization_id)
    slug: Mapped[str]
    name: Mapped[str]
    type: Mapped[str]
    #: Per-type bag validated by `factorial.runtimes.parse_runtime_details`; empty for every
    #: type today, so a new key is a code change rather than a migration.
    details: Mapped[JsonDict] = mapped_column(server_default=text("'{}'::jsonb"))
    #: Operator notes, flat string -> string (`factorial.runtimes.parse_runtime_metadata`). Not
    #: `details`: no adapter reads it and nothing here ever will. The trailing underscore is
    #: SQLAlchemy's — `metadata` is the declarative registry's own attribute.
    metadata_: Mapped[JsonDict] = mapped_column("metadata", server_default=text("'{}'::jsonb"))
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]

    credentials: Mapped[list[RuntimeCredential]] = relationship(
        back_populates="runtime",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )


class RuntimeCredential(Base):
    """`secret`'s twin for a runtime: ciphertext + key id only, AAD-namespaced so a blob from
    the project table never authenticates here even when the integer ids collide."""

    __tablename__ = "runtime_credential"
    __table_args__ = (UniqueConstraint("runtime_id", "name"),)

    id: Mapped[IdPk]
    runtime_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("runtime.id", ondelete="CASCADE")
    )
    name: Mapped[str]
    value_ciphertext: Mapped[bytes]
    enc_key_id: Mapped[str]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]

    runtime: Mapped[Runtime] = relationship(back_populates="credentials", lazy="raise")


class Ticket(Base):
    """One admitted ticket. `current_step` + `status` is the pipeline projection; the lease
    columns are what let N reconciliation loops run without stepping on each other."""

    __tablename__ = "ticket"
    __table_args__ = (
        CheckConstraint(_one_of("status", TICKET_STATUSES), name="status"),
        UniqueConstraint("ticket_source_id", "external_key"),
        Index("ticket_project_status_idx", "project_id", "status"),
    )

    id: Mapped[IdPk]
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("project.id", ondelete="CASCADE")
    )
    ticket_source_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("ticket_source.id"))
    external_key: Mapped[str]
    process_version_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("process_version.id"))
    #: {ticket_owner}: the assignee at admission, stable across later reassignment.
    owner_at_admission: Mapped[str]
    #: The issue title as the last tick observed it — the only issue text the orchestrator keeps,
    #: and a cache, never the truth. NULL means "not observed yet", which a ticket admitted before
    #: revision 0005 stays until its next tick and a `done` one stays forever.
    title: Mapped[str | None]
    current_step: Mapped[str]
    #: Bumped by a restart (decision 67). The loop decides with the current generation's runs
    #: alone; every earlier one is history.
    generation: Mapped[int] = mapped_column(server_default=text("1"))
    status: Mapped[str]
    lease_owner: Mapped[str | None]
    lease_expires_at: Mapped[datetime | None]
    #: The database clock at the last claim. Only a claim writes it: a refresh waits for a claim
    #: later than its request (`POST /tickets/{id}/refresh`), and a renew or release that moved
    #: it would read as a pass that had not happened.
    last_leased_at: Mapped[datetime | None]
    #: Passes that raised in a row. Written from `loop._reconcile_leased`'s error handler in its
    #: own session: the failing pass's transaction is rolled back, so a counter incremented
    #: inside it would vanish with the failure it is trying to count.
    consecutive_failures: Mapped[int] = mapped_column(server_default=text("0"))
    #: Set when `consecutive_failures` crosses `worker.quarantine_after`. Non-NULL means the
    #: loop stops leasing this ticket (decision 31) — the only thing that actually ends the
    #: retry, since a ticket marked BLOCKED without its label on the issue is picked up again.
    quarantined_at: Mapped[datetime | None]
    #: Domain fact, not the audit column — when the ticket entered the process.
    admitted_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    closed_at: Mapped[datetime | None]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]

    project: Mapped[Project] = relationship(back_populates="tickets", lazy="raise")
    step_runs: Mapped[list[StepRun]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )
    events: Mapped[list[Event]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )


class StepRun(Base):
    """One `(ticket, step, attempt)` — agent steps *and* human gates. Token spend and cost
    belong to the run, not to the artifacts it produced."""

    __tablename__ = "step_run"
    __table_args__ = (
        CheckConstraint(_one_of("kind", STEP_KINDS), name="kind"),
        CheckConstraint(_one_of("status", STEP_RUN_STATUSES), name="status"),
        CheckConstraint(_one_of("runtime_kind", RUNTIME_KINDS), name="runtime_kind"),
        CheckConstraint(_one_of("cost_source", COST_SOURCES), name="cost_source"),
        CheckConstraint(_one_of("entered_by", ENTERED_BY), name="entered_by"),
        UniqueConstraint("ticket_id", "step_name", "attempt"),  # idempotent spawns key on this
    )

    id: Mapped[IdPk]
    ticket_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("ticket.id", ondelete="CASCADE"))
    step_name: Mapped[str]
    kind: Mapped[str]
    attempt: Mapped[int] = mapped_column(server_default=text("1"))
    #: The ticket's generation when this row was inserted (`tick_context._insert_run`). Attempt
    #: numbers keep counting across generations, so `(ticket, step, attempt)` stays the key.
    generation: Mapped[int] = mapped_column(server_default=text("1"))
    agent_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("agent.id"))
    #: The only hook into the (deferred) runtime tables.
    runtime_run_id: Mapped[str | None]
    #: The family that ran this, from its runtime row's type — the same value set as
    #: `Agent.runtime_kind`. Written only by `worker.agent_steps._start_attempt`. Gate, action and
    #: check rows leave it NULL: NULL means "no substrate ran this".
    runtime_kind: Mapped[str | None]
    #: The registry row that executed this run; NULL on gates, actions and checks, and on an
    #: agent run from before every process named one. SET NULL on delete so history outlives the
    #: runtime — the API refuses deletion only while a live run still references it.
    runtime_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("runtime.id", ondelete="SET NULL")
    )
    status: Mapped[str]
    failure_reason: Mapped[str | None]
    decided_by: Mapped[str | None]
    #: Who pressed Continue with warning (decision 78). Set only on a SUCCEEDED run; a reader
    #: that counts outcomes splits on it, so a forced step never reads as a success.
    forced_by: Mapped[str | None]
    #: The model id the run was specced with (`RunSpec.model`), stamped at spawn. The substrate's
    #: report never names it, and the estimate below is priced per model.
    model: Mapped[str | None]
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    #: The cache-read part of `input_tokens`, informational: which adapters count it gross is in
    #: `RunResult.cached_input_tokens`.
    cached_input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    #: `CostSource`, or NULL with `cost_usd` when neither the substrate nor the price table
    #: could say. A reader summing `cost_usd` must carry this along: one estimated part makes
    #: the total an estimate.
    cost_source: Mapped[str | None]
    #: `EnteredBy`: rework a gate asked for (`reject_rerun`), a `retry` after a failure or a human
    #: release, or the step's first run (`next`, `admission`). Stamped by
    #: `worker.tick_context._insert_run` from the `step_entered` that predicted this attempt
    #: number. NULL is a row from before revision 0023 that no entry names and that predates its
    #: ticket's first `step_entered`: neither rework nor retry.
    entered_by: Mapped[str | None]
    #: When the run actually began; a spawn is recorded before the container comes up.
    started_at: Mapped[datetime | None]
    ended_at: Mapped[datetime | None]
    #: Seconds a person held the run paused (a stop, an idle or ceiling park), added at each
    #: resume. A resume clears `ended_at` and keeps `started_at`, so active time is
    #: `ended_at - started_at - parked_seconds`. A rollover's park waits on the substrate, not on
    #: a person, and is not counted.
    parked_seconds: Mapped[float] = mapped_column(Double, default=0.0, server_default=text("0"))
    #: Set when a redo set this run aside (decision 76). A current-state reader skips it
    #: (`current_runs`). Attempt numbering, answered reviews, history and spend do not.
    superseded_at: Mapped[datetime | None]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]

    ticket: Mapped[Ticket] = relationship(back_populates="step_runs", lazy="raise")
    artifacts: Mapped[list[Artifact]] = relationship(
        back_populates="step_run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )


def current_runs(generation: int | SQLColumnExpression[int]) -> ColumnElement[bool]:
    """The runs the loop decides with: `generation`'s, less those a redo set aside. A restart
    starts a generation (decision 67); a redo sets runs aside within one (decision 76)."""
    return and_(StepRun.generation == generation, StepRun.superseded_at.is_(None))


class Artifact(Base):
    """Locator metadata; the content lives in GitHub. Append-only, hence no `updated_at`."""

    __tablename__ = "artifact"
    __table_args__ = (
        CheckConstraint(_one_of("direction", ARTIFACT_DIRECTIONS), name="direction"),
        CheckConstraint(_one_of("kind", ARTIFACT_KINDS), name="kind"),
        Index("artifact_step_run_idx", "step_run_id"),
    )

    id: Mapped[IdPk]
    step_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("step_run.id", ondelete="CASCADE")
    )
    direction: Mapped[str]
    kind: Mapped[str]
    #: Free-form so a new artifact kind needs no migration: {branch, path, sha, pr_number, …}.
    locator: Mapped[JsonDict] = mapped_column(server_default=text("'{}'::jsonb"))
    url: Mapped[str | None]
    created_at: Mapped[CreatedAt]

    step_run: Mapped[StepRun] = relationship(back_populates="artifacts", lazy="raise")


class Event(Base):
    """The append-only timeline. Identity `id` gives total order; the dashboard, audit trail
    and cost reports are queries over this table, never separately maintained state."""

    __tablename__ = "event"
    __table_args__ = (
        Index("event_ticket_idx", "ticket_id", "id"),  # per-ticket lifecycle view
        Index("event_project_idx", "project_id", "id"),  # dashboard / audit feed
        # The awake cap and the inspect pass read these lifecycles deployment-wide.
        Index(
            "event_preview_lifecycle_idx",
            "step_run_id",
            "id",
            postgresql_where=text(
                "type IN ('preview_requested', 'preview_stop_requested', 'preview_started', "
                "'preview_failed', 'preview_stopped')"
            ),
        ),
        Index(
            "event_inspect_lifecycle_idx",
            "step_run_id",
            "id",
            postgresql_where=text(
                "type IN ('inspect_requested', 'inspect_started', 'inspect_parked')"
            ),
        ),
        # Delivery metrics count closures per project over a window; `event_project_idx` is
        # keyed on `id` and would scan every event the project has.
        Index(
            "event_closure_idx",
            "project_id",
            "created_at",
            postgresql_where=text("type IN ('ticket_done', 'ticket_exited')"),
        ),
    )

    id: Mapped[IdPk]
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("project.id", ondelete="CASCADE")
    )
    ticket_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("ticket.id", ondelete="CASCADE")
    )
    step_run_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("step_run.id"))
    type: Mapped[str]
    details: Mapped[JsonDict] = mapped_column(server_default=text("'{}'::jsonb"))
    created_at: Mapped[CreatedAt]

    ticket: Mapped[Ticket | None] = relationship(back_populates="events", lazy="raise")


class McpToken(Base):
    """An operator's MCP credential (design.md §7.16). The row is the authority: a JWT's
    signature only finds it, and principal, scopes, expiry and revocation are read from here on
    every request. Never deleted, and not an event: `event.project_id` is NOT NULL and a token
    belongs to no project. No `updated_at`: revocation and last use carry their own times."""

    __tablename__ = "mcp_token"
    __table_args__ = (
        CheckConstraint(_one_of("minted_via", MCP_MINTED_VIAS), name="minted_via"),
        CheckConstraint(_one_of("access", MCP_ACCESSES), name="access"),
        CheckConstraint("(revoked_at IS NULL) = (revoked_by IS NULL)", name="revoked"),
        UniqueConstraint("jti_sha256"),
    )

    id: Mapped[IdPk]
    #: The organization the token acts in, and the only one: its tools see nothing outside it.
    organization_id: Mapped[int] = _organization_fk(index=True)
    #: The JWT carries the raw `jti`; only its sha256 is kept, so a database read plus the API
    #: token cannot re-sign a live token.
    jti_sha256: Mapped[str]
    #: The `aud` at mint. Rotating the API token changes the current audience, which is how a
    #: listing tells `rotated` from `active`.
    audience: Mapped[str]
    label: Mapped[str]
    #: The session's email, or `api-token` for a mint with the shared API token.
    created_by: Mapped[str]
    minted_via: Mapped[str]
    access: Mapped[str]
    created_at: Mapped[CreatedAt]
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    revoked_by: Mapped[str | None]
    last_used_at: Mapped[datetime | None]
    last_used_client: Mapped[str | None]
    last_used_ip: Mapped[str | None]
    #: History: the turn a chat token was minted for, before the assistant's tools ran in its
    #: process. SET NULL: the row outlives its conversation.
    chat_turn_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("chat_turn.id", ondelete="SET NULL")
    )

    @property
    def principal(self) -> str:
        if self.minted_via in (McpMintedVia.SESSION, McpMintedVia.CHAT):
            return self.created_by
        # `api.auth.BEARER_PRINCIPAL`, spelled out: db sits below api.
        return "api-token"

    def scopes(self) -> list[str]:
        # `mcp_server.tokens.READ_SCOPE` / `WRITE_SCOPE`, spelled out: db sits below mcp_server.
        if self.access == McpAccess.WRITE:
            return ["factorial:read", "factorial:write"]
        if self.access == McpAccess.CHAT:
            return ["factorial:read", "factorial:chat"]
        return ["factorial:read"]


class AppSetting(Base):
    """A deployment-wide setting, one JSON document per key: the assistant process's health. The
    assistant's own settings are an organization's (`OrganizationSetting`)."""

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[JsonDict]
    updated_by: Mapped[str]
    updated_at: Mapped[UpdatedAt]


class OrganizationSetting(Base):
    """`app_setting`'s twin for one organization, one JSON document per key: the assistant's
    settings. CASCADE, unlike the organization's other rows: a setting means nothing without it."""

    __tablename__ = "organization_setting"

    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[JsonDict]
    updated_by: Mapped[str]
    updated_at: Mapped[UpdatedAt]


class OrganizationCredential(Base):
    """`secret`'s twin for an organization, AAD `org:<organization_id>:<name>`: a blob copied to
    another organization, or to the project or runtime store, fails to authenticate."""

    __tablename__ = "organization_credential"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)

    id: Mapped[IdPk]
    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id", ondelete="CASCADE")
    )
    name: Mapped[str]
    value_ciphertext: Mapped[bytes]
    enc_key_id: Mapped[str]
    updated_by: Mapped[str]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class AppUser(Base):
    """A dashboard account (design.md §7.19): one per email, stored lowercase because the session
    cookie carries the email and an identity provider may return it in any case. `user` is a
    reserved word in Postgres."""

    __tablename__ = "app_user"
    __table_args__ = (
        UniqueConstraint("email"),
        CheckConstraint("email = lower(email)", name="email_lowercase"),
    )

    id: Mapped[IdPk]
    email: Mapped[str]
    name: Mapped[str]
    #: argon2id. Null for an account that has only signed in by SSO.
    password_hash: Mapped[str | None]
    #: Set by a verified SSO sign-in, a verification link or a password reset: a password proves
    #: nothing about the mailbox.
    email_verified_at: Mapped[datetime | None]
    failed_sign_ins: Mapped[int] = mapped_column(server_default=text("0"))
    locked_until: Mapped[datetime | None]
    #: A session cookie names the epoch it was minted under, and one that is not this is refused.
    session_epoch: Mapped[int] = mapped_column(server_default=text("0"))
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]
    last_sign_in_at: Mapped[datetime | None]


class PermissionRecord(Base):
    """One key of `orchestrator.access.Permission`, the closed catalogue, as reference data a
    role can point at. Named apart from the enum so the two never shadow each other."""

    __tablename__ = "permission"

    key: Mapped[str] = mapped_column(primary_key=True)
    module: Mapped[str]
    action: Mapped[str]
    description: Mapped[str]


class Role(Base):
    """A named set of permissions. A built-in role has no organization and changes only through
    a revision; a custom role, later, names its organization."""

    __tablename__ = "role"
    __table_args__ = (
        UniqueConstraint("organization_id", "key"),
        CheckConstraint("built_in = (organization_id IS NULL)", name="built_in"),
        Index(
            "role_built_in_key_idx",
            "key",
            unique=True,
            postgresql_where=text("organization_id IS NULL"),
        ),
    )

    id: Mapped[IdPk]
    organization_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("organization.id", ondelete="CASCADE")
    )
    key: Mapped[str]
    name: Mapped[str]
    description: Mapped[str]
    built_in: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class RolePermission(Base):
    """RESTRICT from the permission, so a key a role still holds cannot be dropped."""

    __tablename__ = "role_permission"

    role_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("role.id", ondelete="CASCADE"), primary_key=True
    )
    permission_key: Mapped[str] = mapped_column(
        ForeignKey("permission.key", ondelete="RESTRICT"), primary_key=True
    )


class OrganizationMember(Base):
    """Who may act in an organization, and as which role: the tenant boundary (design.md
    §7.19). A change is not an event, so `added_by` names who invited or promoted them."""

    __tablename__ = "organization_member"
    __table_args__ = (Index("organization_member_user_idx", "user_id"),)

    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id", ondelete="RESTRICT"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("role.id", ondelete="RESTRICT"), index=True
    )
    #: An email, or `api-token` for the operator.
    added_by: Mapped[str]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class OrganizationInvite(Base):
    """A link for one email. Only the token's SHA-256 is kept: the token is shown once, in the
    answer that creates it. One pending invite per organization and email."""

    __tablename__ = "organization_invite"
    __table_args__ = (
        UniqueConstraint("token_hash"),
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint("(revoked_at IS NULL) = (revoked_by IS NULL)", name="revoked"),
        CheckConstraint("accepted_at IS NULL OR revoked_at IS NULL", name="settled_once"),
        Index(
            "organization_invite_pending_idx",
            "organization_id",
            "email",
            unique=True,
            postgresql_where=text("accepted_at IS NULL AND revoked_at IS NULL"),
        ),
    )

    id: Mapped[IdPk]
    organization_id: Mapped[int] = _organization_fk()
    email: Mapped[str]
    role_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("role.id", ondelete="RESTRICT"), index=True
    )
    token_hash: Mapped[bytes]
    #: An email, or `api-token`: the operator's first invite to `default` has no account behind it.
    created_by: Mapped[str]
    created_at: Mapped[CreatedAt]
    expires_at: Mapped[datetime]
    accepted_at: Mapped[datetime | None]
    #: SET NULL: an invite outlives the account that took it, as history.
    accepted_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="SET NULL")
    )
    revoked_at: Mapped[datetime | None]
    revoked_by: Mapped[str | None]


class OrganizationRemoval(Base):
    """Who was removed from an organization, or left it: what keeps the allowed-domain SSO join
    from adding them back (design.md §7.19). Only a join that happens by itself reads it; a
    membership is still a deleted row. Keyed by email, as the account and the invite are, so the
    record outlives the account. Accepting an invite, or `grant-owner`, deletes it."""

    __tablename__ = "organization_removal"
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint(_one_of("reason", REMOVAL_REASONS), name="reason"),
    )

    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id", ondelete="RESTRICT"), primary_key=True
    )
    email: Mapped[str] = mapped_column(primary_key=True)
    #: `RemovalReason`.
    reason: Mapped[str]
    #: An email, or `api-token` for the operator.
    removed_by: Mapped[str]
    removed_at: Mapped[datetime]


class EmailVerification(Base):
    """A link that proves the mailbox: `verify` after a password signup, `reset` to set a new
    password (design.md §7.19 item 16). Only the token's SHA-256 is kept. `consumed_at` is set
    when the token is used or a newer one replaces it, so one is live per account and purpose."""

    __tablename__ = "email_verification"
    __table_args__ = (
        UniqueConstraint("token_hash"),
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint(_one_of("purpose", VERIFICATION_PURPOSES), name="purpose"),
        Index(
            "email_verification_live_idx",
            "user_id",
            "purpose",
            unique=True,
            postgresql_where=text("consumed_at IS NULL"),
        ),
    )

    id: Mapped[IdPk]
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), index=True
    )
    #: The address the link went to; a token stops working if it is not the account's.
    email: Mapped[str]
    #: `VerificationPurpose`.
    purpose: Mapped[str]
    token_hash: Mapped[bytes]
    created_at: Mapped[CreatedAt]
    expires_at: Mapped[datetime]
    consumed_at: Mapped[datetime | None]


class ChatConversation(Base):
    """One person's chat, in one organization: its turns run on that organization's assistant
    settings and its tools see that organization alone. Retention counts from `updated_at`: a
    conversation is deleted 30 days after it, and the `chat_item_touch_conversation` trigger
    moves it on every item insert."""

    __tablename__ = "chat_conversation"
    __table_args__ = (
        Index("chat_conversation_owner_idx", "organization_id", "owner", text("updated_at DESC")),
    )

    id: Mapped[IdPk]
    organization_id: Mapped[int] = _organization_fk()
    owner: Mapped[str]
    title: Mapped[str | None]
    backend_kind: Mapped[str]
    backend_session_id: Mapped[str | None]
    archived_at: Mapped[datetime | None]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class ChatTurn(Base):
    __tablename__ = "chat_turn"
    __table_args__ = (
        CheckConstraint(_one_of("status", CHAT_TURN_STATUSES), name="status"),
        CheckConstraint(_one_of("trigger", CHAT_TURN_TRIGGERS), name="trigger"),
        CheckConstraint(_one_of("cost_source", COST_SOURCES), name="cost_source"),
        UniqueConstraint("conversation_id", "seq"),
        Index(
            "chat_turn_one_queued",
            "conversation_id",
            unique=True,
            postgresql_where=text("status = 'queued'"),
        ),
        Index(
            "chat_turn_one_running",
            "conversation_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
        Index("chat_turn_queue_idx", "created_at", postgresql_where=text("status = 'queued'")),
    )

    id: Mapped[IdPk]
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chat_conversation.id", ondelete="CASCADE")
    )
    seq: Mapped[int]
    status: Mapped[str]
    trigger: Mapped[str]
    model: Mapped[str | None]
    lease_owner: Mapped[str | None]
    lease_expires_at: Mapped[datetime | None]
    cancel_requested: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cached_input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    #: As `StepRun.cost_source`: NULL with `cost_usd` when nothing could price the turn.
    cost_source: Mapped[str | None]
    stop_reason: Mapped[str | None]
    failure_reason: Mapped[str | None]
    failure_message: Mapped[str | None]
    started_at: Mapped[datetime | None]
    ended_at: Mapped[datetime | None]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class ChatItem(Base):
    """One thing the Drawer draws. `rev` is stamped by the `chat_item_bump_rev` trigger on every
    insert and update, from one sequence, so a stream resumes from any `rev` it last saw and no
    writer can forget to move it."""

    __tablename__ = "chat_item"
    __table_args__ = (
        CheckConstraint(_one_of("kind", CHAT_ITEM_KINDS), name="kind"),
        Index("chat_item_conversation_rev_idx", "conversation_id", "rev"),
    )

    id: Mapped[IdPk]
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chat_conversation.id", ondelete="CASCADE")
    )
    turn_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("chat_turn.id", ondelete="SET NULL")
    )
    kind: Mapped[str]
    payload: Mapped[JsonDict]
    final: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    rev: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class ChatProposal(Base):
    __tablename__ = "chat_proposal"
    __table_args__ = (CheckConstraint(_one_of("status", CHAT_PROPOSAL_STATUSES), name="status"),)

    id: Mapped[IdPk]
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chat_conversation.id", ondelete="CASCADE")
    )
    turn_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("chat_turn.id", ondelete="SET NULL")
    )
    item_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chat_item.id", ondelete="CASCADE"))
    tool: Mapped[str]
    args: Mapped[JsonDict]
    preview: Mapped[JsonDict]
    status: Mapped[str]
    mcp_token_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("mcp_token.id"))
    expires_at: Mapped[datetime]
    decided_by: Mapped[str | None]
    decided_at: Mapped[datetime | None]
    result: Mapped[JsonDict | None]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class ChatBackendState(Base):
    """The backend's own transcript for a conversation, mirrored here because the child's
    local copy dies with its per-turn config dir."""

    __tablename__ = "chat_backend_state"
    __table_args__ = (UniqueConstraint("conversation_id", "key", "seq"),)

    id: Mapped[IdPk]
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chat_conversation.id", ondelete="CASCADE")
    )
    key: Mapped[str]
    seq: Mapped[int]
    entry: Mapped[JsonDict]
    created_at: Mapped[CreatedAt]


class ChatSpend(Base):
    """One person's estimated assistant spend in one organization on one UTC day, the daily
    budget's ledger: a budget is an organization's, so spend in one never uses another's. Filled
    by the `chat_turn_record_spend` trigger from every change of a turn's `cost_usd`, and kept
    for ever: the turns go with their conversation, the spend does not. No `updated_at`: the
    only change a row sees is its running total."""

    __tablename__ = "chat_spend"

    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id", ondelete="RESTRICT"), primary_key=True
    )
    owner: Mapped[str] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    created_at: Mapped[CreatedAt]
