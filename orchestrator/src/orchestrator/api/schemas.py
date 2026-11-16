"""Wire models for the API.

Separate from both neighbours: the ORM (`orchestrator.db.models`) must be free to change columns
without breaking a client, and the ADLC config models (`orchestrator.config`) are a payload the
API carries, never its own vocabulary.

Keys are snake_case, matching the database and the docs. A config snapshot travelling through
here keeps `process.yaml`'s camelCase aliases and is not re-cased.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from orchestrator.assistant.config import AssistantSettings
from orchestrator.config.lint import LintWarning
from orchestrator.config.loader import MAX_CONFIG_CHARS
from orchestrator.db.models import EnteredBy, RuntimeType
from orchestrator.events.read_models import Outcome
from orchestrator.factorial.runtime_policy import validate_settings
from orchestrator.factorial.runtimes import SLUG_RE

__all__ = [
    "ActivityTurnOut",
    "ApprovalOut",
    "ApproveIn",
    "AssistantBackendOut",
    "AssistantCredentialIn",
    "AssistantCredentialOut",
    "AssistantHealthOut",
    "AssistantReadyOut",
    "AssistantSettingsOut",
    "AssistantStoredCredentialOut",
    "AuthFailureOut",
    "BoardCellOut",
    "BoardResponse",
    "ChangeRequestIn",
    "ChangeRequestOut",
    "ChatConversationIn",
    "ChatConversationOut",
    "ChatConversationPatch",
    "ChatItemOut",
    "ChatItemsOut",
    "ChatMessageAccepted",
    "ChatMessageIn",
    "ChatTurnOut",
    "ClosedTicketOut",
    "ComparedCommitOut",
    "ComparedFileOut",
    "CompletionIn",
    "CompletionOut",
    "ComponentOut",
    "ConfigAdoptionIn",
    "ConfigAdoptionOut",
    "ConfigBranchIn",
    "ConfigBranchOut",
    "ConfigCommitIn",
    "ConfigCommitOut",
    "ConfigEditIn",
    "ConfigFileContentOut",
    "ConfigFileOut",
    "ConfigFilesOut",
    "ConfigPreviewIn",
    "ConfigPreviewOut",
    "CostReport",
    "CredentialCheckOut",
    "CredentialTestOut",
    "DashboardLifetimeOut",
    "DashboardLimitsOut",
    "DefaultBranchConfigOut",
    "DefaultProcessIn",
    "EventFeed",
    "EventOut",
    "ExitIn",
    "ExtremesOut",
    "FileProposalOut",
    "ForgeDisplayOut",
    "GatePullRequestOut",
    "GitHubTokenOut",
    "HealthResponse",
    "InFlightRunOut",
    "InspectOut",
    "InviteAccepted",
    "InviteCreated",
    "InviteIn",
    "InviteOut",
    "InvitePreviewOut",
    "LabelSpecOut",
    "LabelsCreated",
    "LabelsListOut",
    "LanePreviewOut",
    "LargestRunOut",
    "LeadTimeOut",
    "LongestGateOut",
    "McpMinterOut",
    "McpTokenIndex",
    "McpTokenMintIn",
    "McpTokenMinted",
    "McpTokenOut",
    "McpTokenState",
    "McpToolCatalog",
    "McpToolOut",
    "MemberOut",
    "MemberPatch",
    "MostAttemptsOut",
    "OpenGateOut",
    "OrganizationDetail",
    "OrganizationIn",
    "OrganizationSettings",
    "OutcomeCountsOut",
    "OutcomeSpendOut",
    "OverviewProjectOut",
    "OverviewResponse",
    "OverviewTicketOut",
    "PendingIssueOut",
    "PerDoneOut",
    "PeriodFiguresOut",
    "PeriodTicketsOut",
    "PinOut",
    "PredicateOut",
    "PreviewIn",
    "PreviewLinkOut",
    "PreviewOut",
    "ProcessSummary",
    "ProcessSyncOut",
    "ProcessSyncResultOut",
    "ProcessTemplateOut",
    "ProcessTemplatesOut",
    "ProcessValidateIn",
    "ProcessValidateOut",
    "ProcessVersionDetailOut",
    "ProcessVersionOut",
    "ProcessVersionRawOut",
    "ProcessVersionSyncIn",
    "ProjectActiveIn",
    "ProjectDashboardOut",
    "ProjectDetail",
    "ProjectIn",
    "ProjectListItem",
    "ProjectOut",
    "ProjectPatch",
    "ProjectReadiness",
    "ProjectRuntimeRef",
    "ProjectSpendOut",
    "ReadinessItem",
    "ReadyResponse",
    "RestartBlockerOut",
    "RestartIn",
    "RestartPrOut",
    "RestartPreviewOut",
    "RestartRunOut",
    "ReviewOut",
    "RoleOut",
    "RunActivityResponse",
    "RunCountsOut",
    "RuntimeCheckIn",
    "RuntimeCheckOut",
    "RuntimeCredentialIn",
    "RuntimeCredentialList",
    "RuntimeCredentialName",
    "RuntimeCredentialOut",
    "RuntimeIn",
    "RuntimeLoginOut",
    "RuntimeLoginStatusOut",
    "RuntimeOptionsOut",
    "RuntimeOut",
    "RuntimePatch",
    "RuntimeProjectOut",
    "RuntimeProvidedSecret",
    "RuntimeTemplateBuildOut",
    "SecretList",
    "SecretName",
    "SecretValueIn",
    "SlowestDoneOut",
    "Span",
    "SpendBucketOut",
    "SpendFoldOut",
    "StepRunOut",
    "StepSegmentOut",
    "StepTimeOut",
    "TemplateLaneOut",
    "TerminalIdleOut",
    "TerminalOut",
    "TerminalParkedOut",
    "TicketCostOut",
    "TicketDetail",
    "TicketIssueOut",
    "TicketListItem",
    "TicketListResponse",
    "TicketProcessOut",
    "TicketSourceIn",
    "TicketSourceOut",
    "TicketSpendOut",
    "TicketStepCostsOut",
    "TimeSplitOut",
    "TimelineEntryOut",
    "TimelineResponse",
    "ToolCallOut",
    "TurnCredentialIn",
    "TurnCredentialOut",
    "UnknownHeadOut",
    "UnmergedSpendOut",
    "VariantOfferOut",
    "VocabularyOut",
    "WeekOut",
    "WindowSpendOut",
    "checked_repo",
]


class _Out(BaseModel):
    """Response models are built from ORM rows and read-model dataclasses alike."""

    model_config = ConfigDict(from_attributes=True)


#: A sum of money, served as a decimal string. The plain string schema replaces the pattern
#: Pydantic gives `Decimal`, which an MCP client's schema-to-model parse warns it cannot enforce.
Money = Annotated[Decimal, WithJsonSchema({"type": "string"})]
#: `GET /projects/{id}/dashboard?span=`: the last four or twelve weeks, or the project's lifetime.
Span = Literal["4w", "12w", "all"]


# ------------------------------------------------------------------------------- health


class HealthResponse(_Out):
    status: str


class VersionOut(_Out):
    #: The commit the image was built from; null for a build with none, such as a local one.
    revision: str | None


# -------------------------------------------------------------------------------- board


class BoardCellOut(_Out):
    current_step: str
    status: str
    count: int


class InFlightRunOut(_Out):
    step_run_id: int
    ticket_id: int
    external_key: str
    step_name: str
    kind: str
    attempt: int
    status: str
    runtime_run_id: str | None = None
    started_at: datetime | None = None
    created_at: datetime


class ProjectSpendOut(_Out):
    """A project's usage across every attempt (`events.read_models.ProjectSpend`)."""

    input_tokens: int
    output_tokens: int
    cost_usd: Money
    #: True when any summed run's figure is a list-price estimate rather than a measured one
    #: (`StepRunOut.cost_source`): one estimated part makes the whole total an estimate.
    cost_estimated: bool


class BoardResponse(_Out):
    project_id: int
    cells: list[BoardCellOut]
    in_flight: list[InFlightRunOut]
    #: The project's own totals, carried by the board rather than by `ProjectDetail` because this
    #: is the project read the dashboard POLLS. The detail read is fetched once, so a figure
    #: parked on it would sit stale beside rows that keep moving.
    spend: ProjectSpendOut


# ------------------------------------------------------------------------------- tickets


class StepRunOut(_Out):
    id: int
    step_name: str
    kind: str
    attempt: int
    status: str
    failure_reason: str | None = None
    decided_by: str | None = None
    runtime_run_id: str | None = None
    #: The substrate the run's manifest DECLARES (`RUNTIME_KINDS`), not necessarily the one that
    #: executed it — a deployment may run a manifest on a substrate it does not name. The
    #: executing substrate is in the `agent_spawned` event's `runtime`. NULL for gates and actions.
    runtime_kind: str | None = None
    #: The model id the run was specced with, stamped at spawn. NULL for gates and actions.
    model: str | None = None
    input_tokens: int | None = None
    #: The cache-read part of `input_tokens`, when the substrate reported one.
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: Decimal | None = None
    #: `reported` when the substrate measured `cost_usd`, `estimated` when the worker priced the
    #: tokens at list (`worker.pricing`, decision 34), NULL with `cost_usd` when neither could.
    cost_source: str | None = None
    #: Why this attempt exists: `reject_rerun` is rework a gate asked for and `redo` rework a
    #: person did, `retry` a re-run after a failure or a human release, `next` / `admission` /
    #: `restart` the step's first run. NULL for a row older than revision 0023 that no
    #: `step_entered` names.
    entered_by: str | None = None
    #: Who forced the run past its failed conditions (decision 78). The run's `status` then
    #: reads `succeeded`; a reader that shows outcomes splits on this.
    forced_by: str | None = None
    #: Set aside by a redo: history, not where the ticket stands.
    superseded_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    created_at: datetime
    #: The ticket generation the run belongs to (`TicketDetail.generation`). The loop acts on
    #: the current generation's runs only.
    generation: int


class PinOut(_Out):
    """A process snapshot: its row, and the commit it was read from."""

    process_version_id: int
    sha: str


class TicketDetail(_Out):
    id: int
    project_id: int
    #: The project's organization's slug, for org-scoped links.
    organization: str
    external_key: str
    #: The observed issue title, as `TicketListItem` carries it. NULL for a ticket that has not
    #: ticked since revision 0005, so the page falls back to the issue number.
    title: str | None = None
    process_version_id: int
    #: The commit the pinned snapshot was read from.
    process_sha: str
    #: The lane this ticket was pinned into at admission, for the badge beside its name. Read
    #: from the pinned snapshot, so it cannot drift with the project's default or its labels.
    process_key: str
    #: The lane's current snapshot, null once the lane is retired. When it is not the pin,
    #: `POST /tickets/{id}/restart` would move the ticket onto it.
    lane_head: PinOut | None
    #: 1 at admission, and one more per restart. `step_runs` holds every generation's runs.
    generation: int
    owner_at_admission: str
    #: `current_step` + `status` is the pipeline status (data-model.md); issue labels are a
    #: projection of it, never the other way round.
    current_step: str
    status: str
    admitted_at: datetime
    closed_at: datetime | None = None
    #: Non-null means the loop has stopped leasing this ticket (decision 31) — see
    #: `POST /tickets/{id}/requeue`, the operator's way back.
    quarantined_at: datetime | None = None
    #: Passes that raised in a row. Non-zero is why `POST /tickets/{id}/refresh` answers 409.
    consecutive_failures: int = 0
    #: See `TicketListItem.park_reason`.
    park_reason: str | None = None
    #: The step a Redo pressed now would start again (`worker.redos.redo_target`), or null where
    #: Redo is not offered.
    redo_target: str | None = None
    #: The current step's preconditions in words (`predicates.words.describe`), from the pinned
    #: snapshot: what the step waits on before it starts. Names them, never says which is false,
    #: which only an evaluation against GitHub can.
    waiting_on: list[str] = []
    step_runs: list[StepRunOut]


class RequestOut(_Out):
    """Exit, re-enter, restart, interrupt, resume, requeue: recorded, and 202 either way. The
    request waits for the pass it woke, so the page's refetch shows that pass (`api.wakes`)."""

    status: str
    #: A pass that claimed the ticket after the request has released it. False when the wait ran
    #: out first, or when nothing was woken because the ticket is failing.
    landed: bool


class RestartRunOut(_Out):
    """A run a restart cancels, or a parked session it abandons."""

    step: str
    attempt: int
    kind: str


class RestartPrOut(_Out):
    """An open pull request of the ticket's, which a restart closes."""

    number: int
    url: str
    head: str
    base: str


class RestartBlockerOut(_Out):
    """Why a restart would be refused: `done`, `quarantined`, `lane_retired` or `issue_closed`."""

    reason: str
    detail: str


class RestartPreviewOut(_Out):
    """`GET /tickets/{id}/restart/preview`: what a restart would do now, read live off GitHub.
    Advisory: the worker recomputes it from its own observation when it answers the request."""

    #: The ticket's pin, and the lane head a restart lands on: null when the lane is retired.
    current: PinOut
    target: PinOut | None
    #: The pin is the head, so a restart needs `allow_current_pin`.
    current_pin: bool
    #: The latest run at the current step while it is live: the restart cancels it.
    live_run: RestartRunOut | None
    #: An interrupted or idle-parked session at the current step: the restart abandons it.
    parked_run: RestartRunOut | None
    close_prs: list[RestartPrOut]
    delete_branches: list[str]
    #: Pull requests of the ticket's already merged into a branch it does not own, such as
    #: `main`. A warning, not a blocker: a fresh start may repeat that work, and only a person
    #: can say whether it still matters, so the request must carry `allow_merged`.
    merged_outside: list[RestartPrOut]
    blockers: list[RestartBlockerOut]


class PreviewLinkOut(_Out):
    """One published URL. `service` is null for a URL an outside producer published."""

    service: str | None
    url: str


class OutputEntryOut(_Out):
    """One entry of a run's outputs: a file its step declares (`preview.files`, decision 71) as
    the worker recorded it for the run, and the signed link that serves it (decision 58). `kind`
    is an open string, so a reader that meets a kind it does not know can still show the entry by
    its label and links. `pending` until the worker has looked."""

    kind: str
    #: The declared path template; for a file a pattern matched, the template and the path joined
    #: by `#`. It names the entry across attempts.
    key: str
    label: str
    status: Literal["pending", "published", "missing", "ambiguous", "lookup_failed"]
    path: str | None = None
    commit: str | None = None
    size: int | None = None
    #: Over the size the API serves: linked on GitHub, never framed.
    too_large: bool = False
    matches: list[str] = Field(default_factory=list)
    reason: str | None = None
    #: The signed link the frame loads, relative to the API; only for a file it will serve.
    url: str | None = None
    #: The file on the forge: on a GitLab project, its GitLab page. The name predates GitLab.
    github_url: str | None = None
    #: Whether this attempt changed the file; null where the record could not tell.
    changed: bool | None = None
    #: The pattern's label, on every entry a pattern gives; null for a file named exactly.
    group: str | None = None
    #: How many files the pattern matched, shown or not.
    group_total: int | None = None
    #: The pattern's folder on GitHub at the attempt's commit, where every file it matched is.
    group_url: str | None = None


class ChangedFileOut(_Out):
    """A path the step left on its branch, linked on GitHub. Listed so a missing file's reader can
    correct the config; never served."""

    path: str
    github_url: str


class OutputsOut(_Out):
    """A run's outputs (`GET /step-runs/{id}/outputs`): each declared file in the order its pinned
    config declares it. Empty for a step that declares none."""

    step_run_id: int
    entries: list[OutputEntryOut]
    #: The record reached GitHub's comparison limit, so a file may be missing from it.
    cut_off: bool = False
    #: Every path the record holds that is still on the branch, sorted.
    changed_files: list[ChangedFileOut] = Field(default_factory=list)


class PreviewOut(_Out):
    """A step run's preview (`events.previews`). Read by execution identity, never "the latest
    URL on the ticket". `urls` is empty when nothing is published — not yet, or withdrawn."""

    step_run_id: int
    urls: list[PreviewLinkOut]
    status: Literal["none", "starting", "ready", "failed", "stopping", "stopped"]
    #: Why it failed or stopped, from the loop or the sandbox: data, shown as text.
    reason: str | None = None
    #: The run's own sandbox serves this step's preview (`preview.services`, decision 54).
    served_by_runtime: bool = False
    #: Start is offered now: the sandbox is kept and nothing serves.
    startable: bool = False
    #: Why Start is not offered, for a runtime-served step where it is not.
    unavailable: str | None = None
    head_sha: str | None = None
    uncommitted: bool = False
    #: When it closes for idle unless someone keeps it; null while its run is live or nothing
    #: serves.
    stops_at: datetime | None = None
    #: A live session or an open session shares its sandbox: its server reaches only its own
    #: ports.
    beside_session: bool = False
    #: Its sandbox renewed at E2B's cap, and the loop is starting it again.
    renewing: bool = False
    #: Serving now, so Restart is offered.
    restartable: bool = False


class PreviewStartIn(BaseModel):
    """Start, or with `restart` relaunch a serving preview's services in place."""

    restart: bool = False


class TerminalIdleOut(_Out):
    """A session waiting at its prompt with its postconditions unmet."""

    reason: str
    since: datetime | None = None
    #: The agent's question: agent-written text, shown as text.
    question: str | None = None
    #: When the loop acts at the earliest if nobody answers. Typing into the terminal defers it,
    #: and a `retry` also waits for the step's CI to settle.
    parks_at: datetime | None = None
    #: What the loop does then: `park` for a person, or `retry`, ending the run to fail on its
    #: postconditions when nothing was asked and nobody worked with the session.
    at_deadline: Literal["park", "retry"] = "park"


class TerminalParkedOut(_Out):
    """Why a parked session stopped: `idle`, `exited` (its terminal was quit), `rollover`,
    `ceiling`, or `user` for a human's stop."""

    reason: str
    question: str | None = None
    #: The error the stretch's last turn ended in, and the postconditions still unmet when it went
    #: idle or its terminal was quit: data, shown as text.
    turn_error: str | None = None
    unmet: list[str] | None = None
    #: `unmet` in words, for a person; null for a park recorded before the words existed.
    needs: list[str] | None = None
    at: datetime


class InspectOut(_Out):
    """The run's open session (decision 57): `starting` while a request waits for the inspect
    pass, `failed` when that pass refused it or the start failed."""

    status: Literal["none", "starting", "open", "parked", "failed"]
    #: Why the open session parked or was refused, from the loop: a `worker.inspect.ViewClosed`
    #: code, or an older event's sentence.
    reason: str | None = None
    #: The parameters the code is worded with (`closed_by`, `limit_seconds`, `after_seconds`,
    #: `limit`), as the park recorded them.
    reason_details: dict[str, str | int] = {}
    #: Open session is offered now.
    startable: bool = False
    #: Why Open session is not offered, when it is not and no session is starting or open.
    unavailable: str | None = None


class TerminalOut(_Out):
    """What the run's terminal offers (decision 57), by the rule the gateway admits by
    (`events.terminals`): a live session, an open session view, or neither."""

    step_run_id: int
    #: `FACTORIAL_TERMINAL__ENABLED`.
    enabled: bool
    #: The origin the browser dials the gateway on; empty for the API's own
    #: (`Settings.terminal_origin`).
    gateway_origin: str = ""
    kind: Literal["session", "inspect"] | None = None
    #: The gateway would accept a viewer now.
    attachable: bool
    #: Why it would not, as the gateway's close frame says it.
    why_not: str | None = None
    why_not_code: int | None = None
    #: Why a person cannot type into it, when the gateway would accept a viewer.
    read_only: str | None = None
    status: str
    idle: TerminalIdleOut | None = None
    parked: TerminalParkedOut | None = None
    #: Resume is offered: the parked session is the ticket's, and nothing holds it.
    resumable: bool = False
    #: A Resume was pressed and the loop has not answered it yet.
    resume_pending: bool = False
    inspect: InspectOut
    #: When the workspace is released unless the ticket exits first. Null while it is not kept,
    #: or while a run in it is live.
    kept_until: datetime | None = None
    #: The answer cannot change until someone acts: not attachable, no shell or Resume under way,
    #: and no live agent run a terminal could still appear for. A parked session counts as settled:
    #: a person releases it.
    settled: bool
    #: The earliest later run that works in this run's workspace, and its attempt number.
    continued_by: int | None = None
    continued_by_attempt: int | None = None


class TerminalTicketIn(BaseModel):
    #: Ignored: every connection attaches as a viewer and claims the writer seat. Accepted so a
    #: dashboard from before the seat can still ask for a ticket.
    mode: Literal["watch", "type"] | None = None


class TerminalTicketOut(BaseModel):
    """One handshake's credential (decision 57): sent as a subprotocol, never in the URL."""

    ticket: str
    expires_at: datetime


class PreviewIn(BaseModel):
    """What a producer publishes. Null withdraws the URL; validation is `check_preview_url`'s."""

    preview_url: str | None


class ChangeRequestIn(BaseModel):
    """Request changes on a step whose preview was reviewed. `idempotency_key` is chosen by the
    client once per form, so a resubmission — a retry after a lost answer, a double click —
    returns the first submission's outcome instead of recording a second request."""

    feedback: str = Field(min_length=1, max_length=8000)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("feedback")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Feedback must not be blank.")
        return value


class ResumeSessionIn(BaseModel):
    """Resume a parked session. `answer`, when given, is the resumed turn: the
    agent receives it as a human's words, framed as data (decision 26)."""

    answer: str | None = Field(default=None, max_length=8000)

    @field_validator("answer")
    @classmethod
    def _not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("The answer must not be blank. Omit it to resume with nothing typed.")
        return value


class CompletionIn(BaseModel):
    """Force complete: check the run's postconditions now (decision 78). `allow_unmet` asks to
    accept it over the failures in `seen_failures`, which the person read."""

    allow_unmet: bool = False
    seen_failures: list[Annotated[str, Field(max_length=2000)]] = Field(
        default_factory=list, max_length=50
    )


class ApproveIn(BaseModel):
    """Approve a run of a step that awaits review, at the commit the person looked at: refused
    once its branch has moved on."""

    head_sha: str = Field(min_length=1, max_length=64)


class ApprovalOut(_Out):
    approved_by: str
    at: datetime
    head_sha: str


class ReviewOut(_Out):
    """What Approve would approve: the step's branch and its head, the run's latest approval,
    whether Approve applies to the run at all (`offered`: current and waiting at its prompt), and
    `refusal`, why a press now is refused, None when it is accepted. `branch` and `head_sha` are
    None when a refusal short of the branch read is decided, and `pushed` is then False. A step
    that does not await review reads nothing else."""

    awaits_review: bool
    branch: str | None
    head_sha: str | None
    approval: ApprovalOut | None
    offered: bool
    refusal: str | None
    #: The run moved its step's branch from where it stood at spawn, or the branch exists and its
    #: spawn recorded no head for it. For the wording only: it never refuses a press, since a
    #: retry whose earlier attempt pushed everything is still approvable.
    pushed: bool


class RestartIn(BaseModel):
    """Restart a ticket on its lane's current process. `process_version_id` is the lane head the
    caller decided on, refused once the lane has moved; omitted, the head at the time.
    `allow_current_pin` restarts a ticket already on the head; the dashboard never sends it.
    `allow_merged` goes ahead although a pull request of the ticket's is already merged into a
    branch it does not own: the preview's `merged_outside`, which the dashboard asks about."""

    process_version_id: int | None = None
    allow_current_pin: bool = False
    allow_merged: bool = False


class ExitIn(BaseModel):
    """`close_issue` closes the issue as the ticket exits: the gate message's Close and exit
    (decision 79)."""

    close_issue: bool = False


class RedoIn(BaseModel):
    """Redo `target_step` and every step after it from a clean start (decision 76): the step
    `TicketDetail.redo_target` names. `note` is for the fresh agent, which receives it as data."""

    target_step: str = Field(min_length=1)
    note: str | None = Field(None, max_length=8000)

    @field_validator("note")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return (value or "").strip() or None


class ChangeRequestOut(RequestOut):
    """`RequestOut`, plus what became of this request by the time the woken pass landed:
    `accepted` (the gate rejected and the rework is starting), `refused` with the loop's
    `reason`, or `pending` when the pass has not reached it yet."""

    request_id: int
    outcome: Literal["pending", "accepted", "refused"]
    reason: str | None = None


class CompletionOut(RequestOut):
    """`RequestOut`, plus what became of a Force complete: `accepted`, `forced`, `refused` with the
    loop's `reason` and every false leaf in `failures`, or `pending`. `overridable` says a person
    may continue past the refusal, having read them."""

    request_id: int
    outcome: Literal["pending", "accepted", "forced", "refused"]
    reason: str | None
    failures: list[str]
    overridable: bool = False


class RefreshOut(_Out):
    """`POST /tickets/{id}/refresh`: 200 with `landed`, or 202 when the wait ran out first."""

    #: A pass that claimed the ticket after `requested_at` has released it, so `ticket` is what
    #: that pass left.
    landed: bool
    #: The database's clock when the request was made.
    requested_at: datetime
    ticket: TicketDetail


class FleetRefreshOut(_Out):
    """`POST /refresh`: always 202. Nothing marks when a fleet pass is done, so the page's own
    polling shows what it did."""

    #: The database's clock when the request was made.
    requested_at: datetime


class TicketProcessOut(_Out):
    """The config snapshot a ticket pinned at admission — what its process graph is drawn from.

    Carries the body, unlike `ProcessVersionOut`: a snapshot is large and the version list does
    not want it, but a single ticket's graph is exactly the case that does. `content` keeps
    `process.yaml`'s camelCase spelling and is not re-cased (module docstring).
    """

    ticket_id: int
    process_version_id: int
    #: Which lane this ticket runs, and the commit its config was read from — together, what
    #: identifies the steps below for the rest of this ticket's life.
    process_key: str
    source_commit_sha: str
    content: dict[str, Any]


class TicketIssueOut(_Out):
    """The ticket's issue as GitHub has it right now — what the description modal shows.

    Read live per request, never a stored copy (`github.repo_issue`): the issue is GitHub's, the
    orchestrator only observes it, and a body cached at admission would be wrong exactly when
    someone edits the issue to unblock a stuck ticket. `repo` and `number` come back so the client
    can build the issue's own URL without a second source of truth for it.
    """

    ticket_id: int
    repo: str
    number: int
    title: str
    #: The issue body verbatim, markdown and all. Empty string for an issue with no description —
    #: GitHub's `null` is normalised away at the adapter boundary.
    body: str
    #: `open` or `closed`, as GitHub spells it. The ticket's own status is `TicketDetail.status`.
    state: str


class GatePullRequestOut(_Out):
    """The merge/pull request a waiting gate would act on right now, if any.

    Resolved live with the same head/base globs as `worker/gates._gate_step`, not from the
    `gate_waiting` event's frozen `pr_number`.
    """

    ticket_id: int
    number: int | None = None
    url: str | None = None
    head_ref: str | None = None
    #: `closed` and not `merged` is a gate holding on a close (decision 79).
    state: str | None = None
    merged: bool | None = None


class TicketListItem(_Out):
    """One row of a project's ticket table (`events.read_models.TicketRow`)."""

    id: int
    external_key: str
    #: Null for a ticket admitted before the observer stored titles, and for one already `done`
    #: when it shipped — the table shows the issue number alone for those.
    title: str | None = None
    current_step: str
    status: str
    #: Status of the highest attempt at `current_step`, or null when it has none. `status` is
    #: coarser than what the row draws: `blocked` covers an interrupt, a dead-end rejection and an
    #: escalation alike, and only this tells them apart (`events.read_models._latest_run_status`).
    latest_run_status: str | None = None
    #: While that run is parked, why: `idle` (waiting on a human, decision 56), `user` (a human
    #: stopped it), `exited` (its terminal was quit), `rollover` or `ceiling`; null
    #: otherwise. See `events.read_models._park_reason`.
    park_reason: str | None = None
    owner_at_admission: str
    process_version_id: int
    #: The lane this ticket runs, badged on the row. Read from the pinned snapshot, so a ticket
    #: keeps the lane it entered however the project's default or its own labels change later.
    process_key: str
    attempts: int
    #: The same three figures `StepRunOut` carries per attempt, summed over the ticket's. Zero
    #: rather than null for a ticket that has not run: nothing was spent, which is a figure.
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    #: See `ProjectSpendOut.cost_estimated`.
    cost_estimated: bool
    admitted_at: datetime
    closed_at: datetime | None = None
    #: See `TicketDetail.quarantined_at`.
    quarantined_at: datetime | None = None
    updated_at: datetime


class TicketListResponse(_Out):
    project_id: int
    #: Echoed back so a client that filtered can tell a filtered empty list from an empty project.
    status: str | None = None
    open_only: bool = False
    tickets: list[TicketListItem]


class TimelineEntryOut(_Out):
    #: The event's log id — total order, and the client's stable key for the entry.
    id: int
    type: str
    details: dict[str, Any]
    created_at: datetime


class ToolCallOut(_Out):
    tool: str
    target: str | None = None
    #: `None` while the call is still outstanding — a live run's last call has no result yet.
    ok: bool | None = None
    result: str | None = None
    #: `AskUserQuestion` only — see `api.activity` on its closed extractor.
    question: str | None = None
    answer: str | None = None


class ActivityTurnOut(_Out):
    index: int
    #: Free-form model output, unlike tool fields — see `api.activity` on what that costs.
    text: list[str] = []
    thinking: list[str] = []
    tools: list[ToolCallOut] = []


class RunActivityResponse(_Out):
    step_run_id: int
    runtime_run_id: str
    turns: list[ActivityTurnOut]
    milestones: list[str] = []
    outcome: str | None = None
    cost_usd: float | None = None
    truncated: bool


class TimelineResponse(_Out):
    ticket_id: int
    entries: list[TimelineEntryOut]


# -------------------------------------------------------------------------------- events


class EventOut(_Out):
    id: int
    type: str
    ticket_id: int | None = None
    step_run_id: int | None = None
    details: dict[str, Any]
    created_at: datetime


class EventFeed(_Out):
    project_id: int
    #: Echoed back when the feed was filtered to one ticket (the external key the client sent).
    ticket: str | None = None
    events: list[EventOut]
    #: What the poller sends back as `after_id`. `None` means "nothing new yet, reuse your cursor".
    next_after_id: int | None = None


# --------------------------------------------------------------------------------- cost


class TicketCostOut(_Out):
    ticket_id: int
    external_key: str
    cost_usd: Decimal
    tokens: int


class CostReport(_Out):
    project_id: int
    tickets: list[TicketCostOut]
    total_cost_usd: Decimal
    total_tokens: int


# ---------------------------------------------------------------------------- dashboard
#
# `GET /projects/{id}/dashboard` (tech-stack decision 61). Money is `Decimal`, serialized as a
# string like every other cost field; durations are seconds; every sum carries `cost_estimated`
# (data-model.md, `step_run.cost_source`).


class StepTimeOut(_Out):
    """One `(process_key, step, kind)` row of the steps-and-gates table
    (`events.read_models.StepTime`): active run time for an agent, check or action, paused time
    left out; for a gate the wait from opening to decision."""

    process_key: str
    step: str
    kind: str
    runs: int
    median_seconds: float
    #: None under `limits.p90_min_runs` runs, where a 90th percentile is one outlier.
    p90_seconds: float | None = None
    max_seconds: float


class OutcomeCountsOut(_Out):
    """Tickets by what they became. `open` is the complement of the two stored outcomes."""

    done: int
    exited: int
    open: int
    total: int


class PerDoneOut(_Out):
    """Lifetime cost per ticket done (`events.read_models.DoneCost`)."""

    tickets: int
    #: None when nothing is done yet; `—` on the tile, never `$0`.
    mean_usd: Money | None = None
    median_usd: Money | None = None
    #: See `ProjectSpendOut.cost_estimated`; covers both figures.
    cost_estimated: bool


class RunCountsOut(_Out):
    """How runs were priced: the project's lifetime (`events.read_models.ProjectSpend`) or one
    period's (`events.read_models.RunCounts`)."""

    total: int
    #: Runs with a `cost_usd` at all, and how many of those are list-price estimates.
    priced: int
    estimated: int
    #: Runs on a registry runtime of a `*_subscription` type: their dollar figures are API-rate
    #: equivalents, not an invoice (`docs/data-model.md`, cost_source).
    subscription: int
    #: The cache-read part of the matching `spend.input_tokens`.
    cached_input_tokens: int


class DashboardLifetimeOut(_Out):
    """The project's whole history, whatever `span` says. The page's cards read `period`; this
    stays for MCP callers, and the Spend card's all-time line reads it."""

    #: The same read the board carries (`project_spend`), served here too so the tiles and the
    #: sections under them come from one request with one clock.
    spend: ProjectSpendOut
    tickets: OutcomeCountsOut
    per_done: PerDoneOut
    runs: RunCountsOut


class WindowSpendOut(_Out):
    """Usage over a period's runs (`events.read_models.WindowSpend`)."""

    input_tokens: int
    output_tokens: int
    cost_usd: Money
    cost_estimated: bool


class PeriodTicketsOut(_Out):
    """Distinct tickets with that closure in the period, and the tickets admitted in it."""

    done: int
    exited: int
    admitted: int


class LeadTimeOut(_Out):
    """Admission to done over the tickets done in the period, each to its latest done there."""

    tickets: int
    #: None when nothing was done.
    median_seconds: float | None = None


class PeriodFiguresOut(_Out):
    """The cards' figures over one period (`events.read_models.PeriodFigures`). A run counts in
    the period it ended in, or started in while it runs; a closure in the period of its event."""

    spend: WindowSpendOut
    runs: RunCountsOut
    tickets: PeriodTicketsOut
    #: The tickets done in the period, each priced over its whole life.
    per_done: PerDoneOut
    #: Done tickets per 7 days of the period; None for a period under a day.
    velocity_per_week: float | None = None
    lead_time: LeadTimeOut
    #: Runs in the period a person forced past their failed conditions (decision 78).
    forced_steps: int = 0


class TimeSplitOut(_Out):
    """Where the lead time of the tickets done in the window went, each over its whole life
    (`events.read_models.TimeSplit`): agent, check and action runs less their paused time; gate
    waits, cut-short ones included; runs parked for a person; and the rest as idle, floored at
    zero per ticket."""

    tickets: int
    lead_seconds: float
    agent_seconds: float
    gate_seconds: float
    paused_seconds: float
    idle_seconds: float


class OpenGateOut(_Out):
    """A gate waiting for a decision now, whatever the span (`events.read_models.OpenGate`)."""

    ticket_id: int
    external_key: str
    title: str | None = None
    step: str
    attempt: int
    waiting_since: datetime


class OutcomeSpendOut(_Out):
    """Spend by what the ticket became (`events.read_models.OutcomeSpend`)."""

    outcome: Outcome
    #: Under a span, the tickets of the outcome with a run in it; under `all`, every ticket of
    #: it, unrun ones included. The outcome is the ticket's status now.
    tickets: int
    cost_usd: Money
    tokens: int
    cost_estimated: bool


class TicketSpendOut(_Out):
    """One bar of cost by ticket (`events.read_models.TicketSpend`)."""

    ticket_id: int
    external_key: str
    #: See `TicketListItem.title`.
    title: str | None = None
    status: str
    #: `step_run` rows in the span across every step, gates included; under `all` it is the
    #: count `TicketListItem` shows.
    attempts: int
    tokens: int
    cost_usd: Money
    cost_estimated: bool


class SpendFoldOut(_Out):
    """The tickets a top-N list did not draw, in one row (`events.read_models.SpendFold`)."""

    tickets: int
    cost_usd: Money
    cost_estimated: bool


class StepSegmentOut(_Out):
    """One attempt of one step of one ticket, as a segment of its bar
    (`events.read_models.StepCost`)."""

    step: str
    kind: str
    attempt: int
    #: `StepRunOut.entered_by`: `reject_rerun` and `redo` are rework, `retry` a retry; null is a
    #: row from before revision 0023 that no `step_entered` names, and it is neither.
    entered_by: EnteredBy | None = None
    status: str
    #: See `StepRunOut.model` and `StepRunOut.runtime_kind`. NULL on gates and actions.
    model: str | None = None
    runtime_kind: str | None = None
    #: The registry runtime's `type` when the run named one (`RuntimeType`), else NULL.
    runtime_type: str | None = None
    #: Active seconds, `ended_at - started_at` less `parked_seconds`; None while the run is live
    #: or was never started.
    seconds: float | None = None
    #: Where the time view draws the attempt, and the gaps between attempts.
    started_at: datetime | None = None
    ended_at: datetime | None = None
    parked_seconds: float
    tokens: int
    #: Zero, not null, for a gate; the page draws only the agent attempts and lists gates nowhere.
    cost_usd: Money
    cost_estimated: bool
    #: `StepRunOut.forced_by`.
    forced_by: str | None = None
    #: `StepRunOut.superseded_at`.
    superseded_at: datetime | None = None


class TicketStepCostsOut(_Out):
    """Where one ticket's steps spent, segments in run order."""

    ticket_id: int
    external_key: str
    title: str | None = None
    #: Every kind of attempt in the span, while the page draws only agent attempts; the two agree
    #: while checks and actions report no cost.
    cost_usd: Money
    cost_estimated: bool
    segments: list[StepSegmentOut]


class SpendBucketOut(_Out):
    """One rule-defined bucket of spend that produced no merged change
    (`events.read_models.SpendBucket`). `count` is attempts for `rework`, `failed` and
    `set_aside`, tickets for `exited`."""

    count: int
    cost_usd: Money
    cost_estimated: bool


class UnmergedSpendOut(_Out):
    """`events.read_models.UnmergedSpend`: the four rules, and their union."""

    #: Attempts entered by `reject_rerun` or `redo`.
    rework: SpendBucketOut
    #: Every run of a ticket whose status is `exited`.
    exited: SpendBucketOut
    #: Attempts that ended `failed` or `timeout`.
    failed: SpendBucketOut
    #: Agent attempts a redo set aside: work thrown away, never merged.
    set_aside: SpendBucketOut
    #: The four buckets as one set of runs, so a failed attempt on an exited ticket counts once.
    total_usd: Money
    cost_estimated: bool


class ClosedTicketOut(_Out):
    """A ticket that closed in a week (`events.read_models.ClosedTicket`)."""

    ticket_id: int
    external_key: str
    outcome: Literal["done", "exited"]


class WeekOut(_Out):
    """One UTC week starting Monday (`events.read_models.WeekSummary`), zero-filled."""

    week_start: date
    done: int
    exited: int
    #: Spend of the runs the span rule places in the week: `ended_at`, else `started_at`, else
    #: `created_at`.
    cost_usd: Money
    cost_estimated: bool
    #: The tickets done or exited in the week, `done` first, then `exited`, each by key.
    closed: list[ClosedTicketOut]


class LargestRunOut(_Out):
    """The costliest agent run (`events.read_models.LargestRun`)."""

    ticket_id: int
    external_key: str
    title: str | None = None
    step: str
    attempt: int
    model: str | None = None
    cost_usd: Money
    cost_estimated: bool
    tokens: int
    seconds: float | None = None


class MostAttemptsOut(_Out):
    """The `(ticket, step)` with the most attempts (`events.read_models.MostAttempts`)."""

    ticket_id: int
    external_key: str
    title: str | None = None
    step: str
    attempts: int
    #: How many of those were rework after a gate rejection or a redo, how many retries after a
    #: failure, and what the rework cost.
    rework: int
    retries: int
    cost_usd: Money
    rework_cost_usd: Money
    cost_estimated: bool


class LongestGateOut(_Out):
    """The gate that waited longest, to a decision or to the exit that cut it short
    (`events.read_models.LongestGate`)."""

    ticket_id: int
    external_key: str
    title: str | None = None
    step: str
    attempt: int
    status: str
    seconds: float


class SlowestDoneOut(_Out):
    """The ticket done in the span with the longest lead time, and its longest single gate wait
    (`events.read_models.SlowestDone`)."""

    ticket_id: int
    external_key: str
    title: str | None = None
    lead_seconds: float
    #: None when the ticket passed no gate.
    wait_step: str | None = None
    wait_seconds: float | None = None


class ExtremesOut(_Out):
    """The callouts (`events.read_models.RunExtremes`). Each is None when its rule finds nothing,
    and the page draws nothing."""

    largest_run: LargestRunOut | None = None
    most_attempts: MostAttemptsOut | None = None
    longest_gate: LongestGateOut | None = None
    slowest_done: SlowestDoneOut | None = None
    #: The first of `open_gates`: a NOW figure, whatever the span.
    longest_open_wait: OpenGateOut | None = None


class DashboardLimitsOut(_Out):
    """The constants the service cut the lists at, so the page can say "top 12 of 17"."""

    by_ticket: int
    step_costs: int
    open_gates: int
    #: Runs a step or gate needs before its p90 is served.
    p90_min_runs: int


class ProjectDashboardOut(_Out):
    """Every figure the project dashboard draws, in one answer (tech-stack decision 61).

    `period` and every section cover the window, `since` up to `as_of`. Three fields do not:
    `lifetime` is the whole history, and `open_now` and `open_gates` are now. Bounded by
    `limits`, never by the number of runs.
    """

    project_id: int
    span: Span
    #: Monday 00:00 UTC whole weeks back from `as_of`'s week, clipped to the first admission;
    #: the first admission for `all`; `as_of` for a project with no tickets.
    since: datetime
    #: One clock read at the top of the request: the window's end and the figures' freshness.
    as_of: datetime
    #: True when the span reached back before the first ticket, so `since` is its admission.
    clipped: bool
    #: Start of the period of the same length before `since`; None unless the project lived
    #: through all of it, so never for `all` or a clipped window.
    previous_since: datetime | None = None
    lifetime: DashboardLifetimeOut
    period: PeriodFiguresOut
    #: `period` over `[previous_since, since)`, for the change line.
    previous: PeriodFiguresOut | None = None
    #: Tickets neither done nor exited, now.
    open_now: int
    time_split: TimeSplitOut
    #: Gates waiting now, longest first, cut at `limits.open_gates`.
    open_gates: list[OpenGateOut]
    #: Always three rows, in the order done, open, exited, zero-filled.
    by_outcome: list[OutcomeSpendOut]
    #: Tickets that ran in the span, by cost descending, cut at `limits.by_ticket`.
    by_ticket: list[TicketSpendOut]
    #: The rest of the tickets that ran, or None when `by_ticket` holds them all.
    by_ticket_rest: SpendFoldOut | None = None
    #: Admitted tickets that never ran, over the whole lifetime whatever `span` says: nothing
    #: spent, which is a figure. A ticket that ran only before `since` is in neither this nor
    #: `by_ticket`, so under a span the two need not add up to `lifetime.tickets.total`.
    unrun_tickets: int
    #: The first `limits.step_costs` of `by_ticket`, each with its attempts.
    step_costs: list[TicketStepCostsOut]
    #: Ordered by first start in the project.
    steps: list[StepTimeOut]
    gates: list[StepTimeOut]
    unmerged: UnmergedSpendOut
    weeks: list[WeekOut]
    extremes: ExtremesOut
    limits: DashboardLimitsOut


# ----------------------------------------------------------------------------- overview


class OverviewProjectOut(_Out):
    id: int
    name: str
    active: bool


class OverviewTicketOut(_Out):
    ticket_id: int
    project_id: int
    external_key: str
    title: str | None = None
    current_step: str
    status: str
    #: See `TicketListItem.process_key`. The board badges it per row and deliberately does not
    #: band by it — step names are no longer stable within a project, so the bands stay on status.
    process_key: str
    #: See `TicketListItem.latest_run_status`. Here it is what puts an interrupted ticket in the
    #: board's Gate lane instead of beside the escalations in Error.
    latest_run_status: str | None = None
    #: See `TicketListItem.park_reason`.
    park_reason: str | None = None
    runs: int
    attempt: int | None
    cost_usd: Decimal
    #: See `ProjectSpendOut.cost_estimated`.
    cost_estimated: bool
    tokens: int
    minutes: float
    #: See `TicketDetail.quarantined_at`.
    quarantined_at: datetime | None = None


class OverviewResponse(_Out):
    """Every project and every ticket the factory board draws, in one answer."""

    projects: list[OverviewProjectOut]
    tickets: list[OverviewTicketOut]
    done_days: int


# -------------------------------------------------------------------------------- admin
#
# Ordered so nothing is forward-referenced: `ProjectDetail` embeds a ticket source and a process
# version, so those come first.


def _checked_settings[T: (dict[str, Any], dict[str, Any] | None)](value: T) -> T:
    # Refused at the write: a malformed list would otherwise refuse every E2B spawn later.
    if value is not None:
        validate_settings(value)
    return value


# GitHub stays owner/name. GitLab ticket sources may be nested group/sub/project.
GITHUB_REPO_PATTERN = r"^[^/\s]+/[^/\s]+$"
GITLAB_REPO_PATTERN = r"^[^/\s]+(?:/[^/\s]+)+$"


def _gitlab_settings(settings: object) -> bool:
    if not isinstance(settings, dict):
        return False
    if settings.get("forge") == "gitlab":
        return True
    bag = settings.get("gitlab")
    return isinstance(bag, dict) and bool(bag)


def _match_repo(value: str, pattern: str, *, label: str) -> str:
    if re.fullmatch(pattern, value) is None:
        raise ValueError(f"{label} should match the pattern {pattern!r}.")
    return value


def checked_repo(value: str, *, gitlab: bool) -> str:
    """`github_repo` held to its forge's shape: owner/name on GitHub; on GitLab a path of any
    depth, taken from a URL on any host. Raises `ValueError`."""
    if gitlab:
        return _match_repo(_without_host(value), GITLAB_REPO_PATTERN, label="github_repo")
    return _match_repo(value, GITHUB_REPO_PATTERN, label="github_repo")


def _normalize_repo(value: object) -> object:
    """`owner/name` from the shapes people actually paste: a clone URL, an SSH remote, a
    trailing `.git`. Normalized BEFORE the pattern check, because a `.git` that survives strands
    the project behind an App-access check that can never pass."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        text = text.removeprefix(prefix)
    return text.rstrip("/").removesuffix(".git")


def _without_host(repo: str) -> str:
    """A GitLab path from a clone URL or SSH remote on any host. Applied only once the forge is
    known to be GitLab: on GitHub another host's path names someone else's repository."""
    if repo.startswith(("https://", "http://")):
        from urllib.parse import urlsplit

        return urlsplit(repo).path.strip("/").removesuffix(".git")
    if repo.startswith("git@") and ":" in repo:
        return repo.split(":", 1)[1].strip("/").removesuffix(".git")
    return repo


class TicketSourceIn(BaseModel):
    """What the project reacts to. Separate from `github_repo` because intake and artifacts can
    differ (data-model.md)."""

    kind: Literal["github", "gitlab"] = "github"
    external_ref: str = Field(min_length=1)

    _normalize_external_ref = field_validator("external_ref", mode="before")(_normalize_repo)
    #: The *name* of a `secret` row holding the webhook secret, never the secret itself.
    webhook_secret_ref: str | None = None

    @model_validator(mode="after")
    def _check_external_ref(self) -> TicketSourceIn:
        if self.kind == "gitlab":
            self.external_ref = _without_host(self.external_ref)
        pattern = GITLAB_REPO_PATTERN if self.kind == "gitlab" else GITHUB_REPO_PATTERN
        _match_repo(self.external_ref, pattern, label="external_ref")
        return self


class GitHubConnectionIn(BaseModel):
    """A project's GitHub credentials. A secret left blank keeps the stored one; the secrets
    never come back out (`GitHubConnectionOut`)."""

    mode: Literal["app", "token"]
    app_id: str | None = None
    private_key: SecretStr | None = None
    webhook_secret: SecretStr | None = None
    token: SecretStr | None = None
    #: Found from GitHub when left blank.
    bot_login: str | None = None


class GitHubConnectionOut(BaseModel):
    mode: Literal["app", "token"] | None
    app_id: str
    bot_login: str
    installations: dict[str, int]
    private_key_set: bool
    webhook_secret_set: bool
    token_set: bool
    #: Where GitHub is told to deliver this project's webhooks; empty before a mode is chosen.
    webhook_url: str


class TicketSourceOut(_Out):
    id: int
    project_id: int
    kind: str
    external_ref: str
    webhook_secret_ref: str | None = None
    last_polled_at: datetime | None = None
    created_at: datetime


class ProcessVersionSyncIn(BaseModel):
    """Snapshot every lane under `.factorial/processes/` straight out of the project's repo
    (design.md §2.8).

    `ref` is a branch, tag or sha; `None` means the project's `factorial` branch. It is resolved
    to a concrete sha before anything is read, and that sha pins both the snapshot and every
    agent referenced.
    """

    ref: str | None = None


class ProcessVersionOut(_Out):
    id: int
    project_id: int
    #: Which lane this snapshot belongs to — the config's own `name:`.
    process_key: str
    #: The repository and commit this snapshot was read from, and its identity: `(project, repo,
    #: lane, sha)` is unique. Routes still address a row by `id`, which is shorter and already
    #: indexed. A repository other than the project's is a version that is no longer offered.
    source_repo: str
    source_commit_sha: str
    #: When the last sync stopped finding this lane's file, or a sync of another repository ran. A
    #: retired lane keeps every snapshot it had; it is simply not selectable (design.md §4). NULL
    #: is offered only while `source_repo` is the project's repository.
    retired_at: datetime | None = None
    #: The ref the sync asked for, `factorial` when none was named; NULL = the repo's default
    #: branch (rows before issue #248). The version table shows it,
    #: and the sync form offers the newest one back rather than making an operator retype it.
    source_ref: str | None = None
    #: Who adopted this snapshot's commit, when it was a direct push an operator vouched for.
    adopted_by: str | None = None
    #: When a sync last landed on this snapshot; the newest per lane is current.
    activated_at: datetime
    created_at: datetime


class ProcessSyncResultOut(BaseModel):
    """What one sync did to one lane: `created`, `unchanged` or `retired` (decision 38)."""

    process: str
    outcome: Literal["created", "unchanged", "retired"]
    #: The row to address in `/process-versions/{version_id}`; absent for `retired`.
    version_id: int | None = None


class DefaultProcessMovedOut(BaseModel):
    """The default lane this sync re-pointed, because the commit no longer offers the old one.

    Present only when it happened. The alternative was refusing the sync until the operator
    changed the setting, which the Process config page cannot do for a lane the repo has retired.
    """

    previous: str
    now: str


class ProcessSyncOut(BaseModel):
    """One sha, one row per lane, plus what the snapshots could not prove.

    Still a 201: sync's contract is that every process parsed and its agents were pinned. Whether
    those agents have usable manifests is a different question, and one it never asked before —
    `upsert_agent_rows` pins by convention, without reading a manifest, so a repo whose `agents/`
    folder is missing or half-written synced clean and failed at the first spawn.
    """

    sha: str
    results: list[ProcessSyncResultOut]
    #: One line per referenced agent that does not resolve at the synced sha; empty means sound.
    #: Deduplicated across lanes, which usually share agents. Absent and present-but-unparseable
    #: read the same here, on purpose: an unfilled scaffolded stub is the likelier failure,
    #: because the file existing looks like completion.
    agent_warnings: list[str] = Field(default_factory=list)
    #: Set when the commit retired the lane `project.settings.default_process` named, and the
    #: sync moved the default to one the commit offers. Null when the default did not move.
    default_process_moved: DefaultProcessMovedOut | None = None


class ProcessSummary(BaseModel):
    """One available lane, as the Process config page and the label help list it."""

    process_key: str
    #: True for the lane `project.settings.default_process` names — what an issue carrying no
    #: process label enters.
    is_default: bool
    #: The exact label that selects this lane, so it can be copied rather than retyped.
    selection_label: str
    latest: ProcessVersionOut


class DefaultProcessIn(BaseModel):
    """`null` clears the default; a project with lanes and no default requires every issue to
    carry its own process label."""

    process_key: str | None = None


class ListChange(BaseModel):
    """Entries to add to and remove from one settings list, applied to its current value."""

    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


class PermitsChange(BaseModel):
    """A change to a project's E2B permits (decision 25), applied on the server to the settings
    as they stand. The page sends what changed, never the whole object, so two saves in flight
    cannot overwrite each other and an Undo is the reverse change."""

    runtime_allowlist: ListChange | None = None
    egress_allowlist: ListChange | None = None


class PendingIssueOut(BaseModel):
    """One open issue the admission pass declines, and why.

    Recomputed per request, never read back from the event log: an append-only row says what
    happened and cannot say whether it is still happening, which is the only question an operator
    waiting on their issue is asking.
    """

    external_key: str
    title: str
    #: The lane the issue selected, when selection got that far. `None` for a selection refusal.
    process_key: str | None = None
    #: `not_ready` is the ordinary state of an issue nobody has marked for the factory; the other
    #: three are misconfiguration and each also appends one `admission_refused` event.
    reason: Literal["no_default", "unknown_process", "ambiguous_process", "not_ready"]
    detail: str
    #: The leaves of the lane's `entry.when` that are still false, one line each. Empty for the
    #: three refusals, which never reached a lane whose entry condition could be evaluated.
    awaiting: list[str] = []
    #: `awaiting` in words (`predicates.needs`), for a person.
    needs: list[str] = []


class ProcessVersionDetailOut(ProcessVersionOut):
    """One version WITH its snapshot — the list route omits bodies on purpose."""

    content: dict[str, Any]


class ProcessVersionRawOut(BaseModel):
    """The version as YAML text. `repo` = the file at the pinned commit, comments and all;
    `snapshot` = the stored parse re-emitted, when there is no pin or no App to read with."""

    content: str
    source: Literal["repo", "snapshot"]


class ConfigFileOut(BaseModel):
    """One file of the pinned `.factorial/`, with what the explorer draws beside its name."""

    path: str
    kind: Literal["process", "agent", "prompt", "skill", "mcp", "other"]
    #: How this file stands against the repo's head. `unknown` when the head could not be read —
    #: never an optimistic `same`, because a silent "no drift" is the one wrong answer here.
    drift: Literal["same", "differs", "added", "removed", "unknown"]
    #: Why this file cannot be used, if it cannot. Only agent manifests carry one today, and only
    #: for agents the pinned process actually names.
    warning: str | None = None
    #: The file's git blob at the pinned commit, which an edit sends back as its base. `None` for a
    #: file only the head has.
    sha: str | None = None


class ConfigFilesOut(BaseModel):
    """The pinned config folder as a flat list; the client nests it by path segment."""

    config_dir: str
    pinned_sha: str
    #: `None` when the head could not be read, which is also when every `drift` is `unknown`.
    head_sha: str | None = None
    files: list[ConfigFileOut]


class AgentDetailOut(BaseModel):
    """One agent a version's process names, read at its pinned commit. When the manifest does not
    resolve, only `name` and `warning` are set."""

    name: str
    warning: str | None = None
    model: str | None = None
    max_turns: int | None = None
    skills_local: list[str] = Field(default_factory=list)
    skills_shared: list[str] = Field(default_factory=list)
    mcp_local: list[str] = Field(default_factory=list)
    mcp_shared: list[str] = Field(default_factory=list)
    secrets: list[str] = Field(default_factory=list)
    #: `None` when the manifest asks for nothing, which means the default profile — not no access.
    permissions: dict[str, str] | None = None


class ConfigFileContentOut(BaseModel):
    """One config file's text at the pinned commit."""

    path: str
    content: str


class LabelSpecOut(BaseModel):
    name: str
    color: str
    description: str
    #: None when the project has no GitHub connection: expected, not verified.
    exists: bool | None = None


class LabelsListOut(BaseModel):
    labels: list[LabelSpecOut]


class ProcessValidateIn(BaseModel):
    """Raw `process.yaml` text, validated without storing anything."""

    content: str = Field(min_length=1, max_length=MAX_CONFIG_CHARS)


class ProcessValidateOut(BaseModel):
    """errors come from the parser (the config is unusable); warnings from the lint layer
    (the config parses green and would misbehave)."""

    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[LintWarning] = Field(default_factory=list)
    #: The parsed echo when valid — what previews draw from, so they render the server's read
    #: of the text rather than the client's.
    content: dict[str, Any] | None = None


class FileProposalOut(BaseModel):
    """One file the catalog could contribute, with everything the explorer needs to draw it."""

    path: str
    body: str
    component: str
    kind: Literal["agent", "skill", "mcp"]
    variant: str | None = None
    version: int
    #: The markers this file used and their values. The client compares them after a config
    #: edit to decide whether a body it has edited has gone stale; the server never trusts the
    #: copy that comes back, because it recomputes the proposal at submit anyway.
    facts: dict[str, str] = Field(default_factory=dict)
    #: Already in the target repo. The catalog never writes it; the operator changes it as an edit
    #: of the repo's own copy (`ConfigCommitIn.edits`).
    present_in_repo: bool = False


class VariantOfferOut(BaseModel):
    variant: str
    summary: str
    offered: bool
    default_on: bool
    reason: str | None = None
    paths: list[str] = Field(default_factory=list)


class ComponentOut(BaseModel):
    name: str
    kind: Literal["agent", "skill", "mcp"]
    variants: list[VariantOfferOut] = Field(default_factory=list)
    present_in_repo: bool = False
    #: The config names this agent. A shared skill or MCP server is here because a manifest
    #: declares it, which must read differently.
    referenced: bool = True


class ConfigPreviewIn(BaseModel):
    """Draft `process.yaml` to plan a scaffold from. Nothing is written."""

    content: str = Field(min_length=1, max_length=MAX_CONFIG_CHARS)


class ConfigPreviewOut(BaseModel):
    """What a commit of this content would create, before the operator commits to it."""

    files: list[FileProposalOut]
    components: list[ComponentOut]


class SelectedFileIn(BaseModel):
    path: str = Field(min_length=1)
    body: str = Field(max_length=MAX_CONFIG_CHARS)


class ConfigEditIn(BaseModel):
    """A file the config branch already has, rewritten by the operator. `base_sha` is the git blob
    the edit started from; the commit is refused if the branch no longer holds it."""

    path: str = Field(min_length=1)
    body: str = Field(max_length=MAX_CONFIG_CHARS)
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class ConfigDeleteIn(BaseModel):
    """A file the config branch has, removed by the operator. `base_sha` is the git blob the
    operator saw; the commit is refused if the branch holds another."""

    path: str = Field(min_length=1)
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class ConfigCommitIn(BaseModel):
    """The `process.yaml` to commit, and the files the operator assembled.

    `content` is committed byte for byte. `files` may name only paths the server's own proposal
    for `content` contains (tech-stack decision 33); the process file always comes from `content`,
    so the committed config is the one that was parsed. `edits` may name only files the branch
    holds under `.factorial/`, each at the blob it started from. `created` may name only paths
    under `.factorial/` made of safe names, where the branch has nothing at, above or inside.
    `deleted` may name only paths under `.factorial/`; one the branch holds must be at the blob
    it names, and one the branch no longer holds is skipped.
    """

    content: str = Field(min_length=1, max_length=MAX_CONFIG_CHARS)
    files: list[SelectedFileIn] = Field(default_factory=list)
    edits: list[ConfigEditIn] = Field(default_factory=list)
    created: list[SelectedFileIn] = Field(default_factory=list)
    deleted: list[ConfigDeleteIn] = Field(default_factory=list)


class RestorePreviewOut(BaseModel):
    """What restoring the config folder to a synced version would do to the branch head."""

    target_sha: str
    head_sha: str
    writes: list[str]
    deletes: list[str]
    #: Processes the head offers and the version does not: the sync retires them.
    retires: list[str]
    #: The head already holds the version's folder.
    unchanged: bool


class ConfigRestoreIn(BaseModel):
    """A version row to restore the folder to, and the head the preview was drawn against. The
    request names no path and no commit: both come from the row."""

    version_id: int = Field(ge=1)
    expected_head: str = Field(pattern=r"^[0-9a-f]{40}$")


class DraftProblemOut(BaseModel):
    #: The config file the problem is in; null for the process text itself, or for a refusal that
    #: is about the branch.
    path: str | None = None
    message: str
    hint: str | None = None


class DraftCheckOut(BaseModel):
    """A commit's own checks on a draft, as a verdict. `valid` means the commit would be made."""

    valid: bool
    problems: list[DraftProblemOut] = Field(default_factory=list)
    warnings: list[LintWarning] = Field(default_factory=list)
    would_write: list[str] = Field(default_factory=list)
    would_delete: list[str] = Field(default_factory=list)


class ConfigBranchIn(BaseModel):
    """What a seed starts from: the default branch's config folder, or nothing, for a template."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    from_: Literal["default_branch", "empty"] = Field(default="default_branch", alias="from")


class ConfigBranchOut(BaseModel):
    """The config branch a seed created, and the orphan commit it points at."""

    branch: str
    sha: str


class ComparedCommitOut(BaseModel):
    sha: str
    message: str
    #: The pusher's own git config: an agent sets it freely, so the page labels it claimed.
    author_name: str | None
    authored_at: str | None


class ComparedFileOut(BaseModel):
    path: str
    status: str
    additions: int
    deletions: int
    previous_path: str | None = None


class UnknownHeadOut(BaseModel):
    """What adopting the `factorial` head would vouch for: every commit since the newest one the
    sync guard accepts."""

    head_sha: str
    #: None when no factory commit sits under the head: the person is vouching for the tip alone.
    known_base: str | None = None
    commits: list[ComparedCommitOut]
    files: list[ComparedFileOut]
    total_commits: int
    #: The compare API lists at most 300 files; the compare link shows the rest.
    files_truncated: bool
    compare_url: str


class ConfigAdoptionIn(BaseModel):
    #: The head the person reviewed, in full. A short sha is refused: 28 bits is guessable.
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class ConfigAdoptionOut(BaseModel):
    sha: str
    #: False when the head was already known; nothing was recorded and it was synced again.
    adopted: bool
    #: Null when the sync failed after the adoption was recorded; `sync_error` says why.
    sync: ProcessSyncOut | None = None
    sync_error: str | None = None


class ConfigCommitOut(BaseModel):
    """The builder's commit on the config branch, and the sync of exactly that commit."""

    sha: str
    url: str
    branch: str
    #: False when the branch already held the draft: nothing was written and `sha` is the head
    #: that was synced again.
    committed: bool
    components: list[ComponentOut] = Field(default_factory=list)
    #: Every path the commit wrote, stubs included.
    written: list[str] = Field(default_factory=list)
    #: Every path the commit removed.
    deleted: list[str] = Field(default_factory=list)
    #: Null when the sync failed after the commit landed; `sync_error` then says why, and the
    #: operator can sync again from the configuration page.
    sync: ProcessSyncOut | None = None
    sync_error: str | None = None


class TemplateLaneOut(BaseModel):
    """One lane of a template: one process file of its folder."""

    key: str
    #: The whole process file, comments included. The editor sets its own `runtime:` line on it.
    yaml: str
    #: The parse, in a version snapshot's shape, so the gallery draws it with the same graph.
    content: dict[str, Any]
    #: What a first commit of it would write for each agent it names.
    agents: list[AgentDetailOut]


class ProcessTemplateOut(BaseModel):
    key: str
    title: str
    description: str
    #: Every process file in the template's folder, in name order; an import writes them all.
    lanes: list[TemplateLaneOut]


class LanePreviewOut(BaseModel):
    key: str
    content: dict[str, Any]
    agents: list[AgentDetailOut]


class DefaultBranchConfigOut(BaseModel):
    """The config folder the default branch's head holds, which a seed would copy unchanged."""

    branch: str
    sha: str
    config_dir: str
    #: Empty when `error` is set.
    lanes: list[LanePreviewOut]
    #: Why the folder does not parse. The copy is still allowed; the editor is where it is fixed.
    error: str | None = None


class ProcessTemplatesOut(BaseModel):
    templates: list[ProcessTemplateOut]
    #: What a new agent, skill, MCP server or process starts from: kind to file to body, with
    #: `__NAME__` where the typed name goes.
    shells: dict[str, dict[str, str]] = Field(default_factory=dict)


class PredicateOut(BaseModel):
    name: str
    min_args: int
    max_args: int


class VocabularyOut(BaseModel):
    """The closed registries, for pickers — config may only reference these."""

    predicates: list[PredicateOut]
    actions: list[str]
    placeholders: list[str]
    input_sources: list[str]
    compositions: list[str]


class SecretDeliveryIn(BaseModel):
    """How a run is supplied a secret (tech-stack decision 75): `env`, the default, or `header`
    with both egress fields."""

    sandbox_delivery: Literal["header", "env"] = "env"
    egress_host: str | None = None
    egress_header: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,64}$")

    @model_validator(mode="after")
    def _route_matches_delivery(self) -> SecretDeliveryIn:
        routed = self.egress_host is not None or self.egress_header is not None
        if self.sandbox_delivery == "header":
            if self.egress_host is None or self.egress_header is None:
                raise ValueError("A header delivery needs both egress_host and egress_header.")
        elif routed:
            raise ValueError("egress_host and egress_header belong to a header delivery only.")
        return self


class SecretValueIn(SecretDeliveryIn):
    """Write-only. The value is encrypted on arrival and never read back out over the API. A
    write replaces the whole row, its delivery included."""

    value: str = Field(min_length=1)


class SecretName(BaseModel):
    name: str


class SecretDeliveryOut(BaseModel):
    """A project secret's sandbox delivery. Never the value."""

    name: str
    mode: Literal["header", "env"]
    host: str | None = None
    header: str | None = None


class SecretList(BaseModel):
    names: list[str]
    deliveries: list[SecretDeliveryOut] = []


class RuntimeProvidedSecret(BaseModel):
    """A declared key the process's runtime owns outright (tech-stack decision 36): the project
    store is never read for it, so the page labels the row instead of asking for a value."""

    name: str
    runtime_slug: str
    #: Whether the runtime can supply it. False is the readiness row's blocker. A
    #: platform-minted type reports True without a stored row.
    stored: bool
    #: Open tickets pinned to a snapshot that still takes this key from the project store — one
    #: naming no runtime, or a runtime whose type owns a different key. Config is pinned per
    #: ticket, so the latest version naming the runtime does not retire the copy for them.
    open_tickets_reading_copy: int = 0


class OrganizationSettings(BaseModel):
    """`organization.settings`, validated on read and on write (design.md §7.19). No fields yet:
    the first organization-level customization is one, and `extra="forbid"` keeps anything else
    out until it is."""

    model_config = ConfigDict(extra="forbid")


class OrganizationOut(_Out):
    """An organization (design.md §2.11). `slug` is the URL's key and never changes. `role` is
    the caller's, null for the operator and while multi-org is off (design.md §7.19)."""

    slug: str
    name: str
    project_count: int
    runtime_count: int
    updated_by: str
    created_at: datetime
    updated_at: datetime
    role: str | None = None


class OrganizationDetail(OrganizationOut):
    """One organization, with what the caller may do in it, so the dashboard hides the rest.
    `settings` is null for a caller who cannot change them."""

    settings: OrganizationSettings | None
    permissions: list[str]


class OrganizationPatch(BaseModel):
    """The name only: the slug is in every org-scoped URL and every link a person kept."""

    name: str = Field(min_length=1, max_length=200)


class OrganizationIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=3, max_length=40)


class RoleOut(BaseModel):
    key: str
    name: str
    description: str
    permissions: list[str]


class PermissionOut(BaseModel):
    key: str
    description: str


class MemberOut(_Out):
    user_id: int
    email: str
    name: str
    role: str
    #: False for an account that has only signed in with a password: nothing proved its email
    #: (design.md §7.19, accepted cost 1).
    verified: bool
    added_by: str
    created_at: datetime


class MemberPatch(BaseModel):
    role: str = Field(min_length=1, max_length=64)


class InviteIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: str = Field(min_length=1, max_length=64)


class InviteOut(BaseModel):
    id: int
    email: str
    role: str
    created_by: str
    created_at: datetime
    expires_at: datetime


class InviteCreated(InviteOut):
    """The link is in this answer only: the database keeps the token's digest."""

    path: str
    url: str | None


class InvitePreviewOut(BaseModel):
    organization_slug: str
    organization_name: str
    role: str
    created_by: str
    expires_at: datetime
    for_you: bool


class InviteAccepted(BaseModel):
    organization_slug: str
    role: str


class ProjectIn(BaseModel):
    name: str = Field(min_length=1)
    github_repo: str = Field(min_length=1)

    _normalize_github_repo = field_validator("github_repo", mode="before")(_normalize_repo)

    @model_validator(mode="after")
    def _check_github_repo(self) -> ProjectIn:
        self.github_repo = checked_repo(self.github_repo, gitlab=_gitlab_settings(self.settings))
        return self

    description: str | None = None
    #: Project-level knobs (data-model.md "Project settings"), among them `mcp_allowlist`,
    #: `permission_ceiling` and `default_process`. Extra keys are allowed so new knobs need no
    #: migration.
    #: `mcp_allowlist` is a boundary rather than a knob: it is what permits a repo's declared MCP
    #: servers to run at all (design.md §7.5).
    settings: dict[str, Any] = Field(default_factory=dict)

    _check_settings = field_validator("settings")(_checked_settings)


class ProjectPatch(BaseModel):
    """Partial update. Every field is optional and `None` means "leave it alone", so there is no
    way to clear `description` — an explicit empty string does that."""

    name: str | None = Field(default=None, min_length=1)
    description: str | None = None
    #: Shape-checked by `update_project` with `checked_repo`: which forge's shape applies is the
    #: project's ticket source's to say, and the body does not carry it.
    github_repo: str | None = Field(default=None, min_length=1)

    _normalize_github_repo = field_validator("github_repo", mode="before")(_normalize_repo)
    settings: dict[str, Any] | None = None

    _check_settings = field_validator("settings")(_checked_settings)


class ForgeDisplayOut(_Out):
    """Link templates and nouns for the project's code host. The UI uses this instead of
    branching on the forge kind."""

    kind: str
    name: str
    origin: str
    pull_noun: str
    issue_path: str
    pull_path: str
    tree_path: str
    commit_path: str


class ProjectOut(_Out):
    id: int
    #: The owning organization's slug (`Project.organization_slug`), for org-scoped links.
    organization: str = Field(validation_alias=AliasChoices("organization", "organization_slug"))
    name: str
    github_repo: str
    description: str | None = None
    settings: dict[str, Any]
    #: The admission switch (db.models.Project.active) — not "is anything running".
    active: bool
    created_at: datetime
    forge: ForgeDisplayOut | None = None


class ProjectRuntimeRef(_Out):
    """The runtime a project's latest process version names (`api.bindings`). `name` is None
    when no Runtime row carries the slug any more: the list still shows what the process says,
    and nothing counts it."""

    slug: str
    name: str | None


class ProjectListItem(ProjectOut):
    runtime: ProjectRuntimeRef | None = None
    #: What the project reacts to, so the repository link opens that forge. None means GitHub,
    #: which is what a project with no source is.
    ticket_source: TicketSourceOut | None = None


#: The requirements the configuration page renders a row for, in the order it renders them: the
#: App's access, the config branch and its rules, the ones the orchestrator owns, then the ones
#: that live in the target repo.
ReadinessKey = Literal[
    "app_access",
    "config_branch",
    "config_branch_rules",
    "ticket_source",
    "github_connection",
    "github_webhook",
    "process_config",
    "secrets",
    "runtime",
    "labels",
    "branch_protection",
    "ci_workflows",
    "gate_approvers",
]


class ReadinessCheck(BaseModel):
    """One rule a requirement holds, read on its own: a branch protection setting, say. Present
    only when the read succeeded, so a check is a pass or a fail and never a shrug.

    `remedy` fixes this check and no other, so running it never rewrites a setting someone made
    on purpose.
    """

    key: str
    title: str
    description: str
    passed: bool
    remedy: str | None = None
    link: str | None = None


class ReadinessItem(BaseModel):
    """One requirement's verdict, shaped for a checklist rather than a warning box.

    Four states, not two. `ok` and `blocked` are the verdicts `blockers` already carried; `warn`
    is a real defect that provably does not stop admission (a missing label — bootstrap step
    6 — is not provably fatal); `unknown` is the one the prose lists could not express. Every
    GitHub-backed probe degrades rather than raising, so "the App is unconfigured" and "GitHub
    timed out" have to render as neither a tick nor a cross. Collapsing `unknown` into `blocked`
    would report an outage as a defect and send someone to fix working config.

    `summary` is the one-line state for the row; `detail` carries the old prose and is set only
    when something is wrong; `remedy` is a shell command to copy, for the rows whose fix lives
    outside the orchestrator's granted permissions.
    """

    key: ReadinessKey
    #: Which lane this row is about, when the requirement is lane-specific. Only `runtime` sets
    #: one: an unusable Runtime stops its own lane and no other, so blurring several lanes into
    #: one verdict would hide a broken lane or block a whole project over an unused one. Rows are
    #: therefore identified by `(key, process)`, not by `key` alone.
    process: str | None = None
    #: The slug that process names. Only `runtime` rows set one.
    runtime_slug: str | None = None
    state: Literal["ok", "blocked", "warn", "unknown"]
    summary: str
    detail: str | None = None
    remedy: str | None = None
    #: Where a human goes to fix this, when that place is a GitHub settings page rather than a
    #: command. Only `app_access` sets one: installing an App and choosing its repositories is
    #: not something the App can do for itself.
    link: str | None = None
    #: The rules this row is the verdict of, each with its own fix. Only the two branch rows set
    #: them: a protection rule has several settings, and a reader fixes one at a time.
    checks: list[ReadinessCheck] = Field(default_factory=list)


class LabelsCreated(BaseModel):
    """What `POST /projects/{id}/labels` actually created — never what it expected to."""

    created: list[str]


class ProjectDetail(ProjectOut):
    """A project plus what the UI needs to decide whether it can be activated.

    `blockers` is the readiness verdict — empty means `PUT /active {true}` will succeed —
    computed the same way the activation endpoint checks. `warnings` is advisory, best-effort
    facts ("not checked" notes when a pinned manifest cannot be read) that never gate
    activation, which is why they are a separate list.

    The rows that read live repo state answer on `GET /projects/{id}/readiness` instead, so the
    page's first paint does not wait on GitHub. They are absent here, never empty-valued.
    """

    ticket_source: TicketSourceOut | None = None
    #: Every lane a ticket could be admitted into right now, each with the version it would pin.
    #: Retired lanes are absent: version history is not a menu (design.md §4).
    processes: list[ProcessSummary] = Field(default_factory=list)
    #: The lane an issue carrying no process label enters. `None` means every issue must name
    #: one, which is allowed and is what an operator gets after clearing the default.
    default_process: str | None = None
    ticket_count: int
    open_ticket_count: int
    #: Tickets sent off the floor (decision 29). Counted apart from `open_ticket_count`
    #: because an exit sets `closed_at`, so without this the caller's
    #: `ticket_count - open_ticket_count` reads every exited ticket as done.
    exited_ticket_count: int
    secret_names: list[str]
    #: Every key the pinned manifests declare (`GH_TOKEN` included), from the same probe that
    #: computes the secrets blocker — so the page can draw one row per key with its provenance.
    declared_secrets: list[str] = Field(default_factory=list)
    #: The vendor key the named runtime provides, when the process names one that exists.
    runtime_provided_secrets: list[RuntimeProvidedSecret] = Field(default_factory=list)
    #: How each stored key is supplied to a run.
    secret_deliveries: list[SecretDeliveryOut] = Field(default_factory=list)
    blockers: list[str]
    warnings: list[str] = Field(default_factory=list)
    #: The repo-side rows `GET /readiness` will return, so the page holds their places from its
    #: first paint instead of inserting rows when the probes answer.
    probe_keys: list[ReadinessKey] = Field(default_factory=list)
    #: The same facts as `blockers` + `warnings`, one row per requirement including the passing
    #: ones. `blockers` is a projection of these rows, not a parallel computation — see
    #: `readiness.blockers`. Repo-side rows are on `ProjectReadiness`.
    readiness: list[ReadinessItem] = Field(default_factory=list)


class ProjectReadiness(BaseModel):
    """The readiness rows whose verdict comes from a live GitHub read, and their notes.

    Its own response because it is its own wait: each costs a round trip and none of them can
    refuse an activation, so making the project page hold for them bought a slower first paint
    and no extra certainty. Only `app_access` reaches `blocked`, for a repo outside the
    installation, and that still gates nothing. The configuration pages ask for them; nothing
    else needs them.

    Shaped like the fields it was cut from — a row list and a warning list — so a reader of
    either payload reads the same two things.
    """

    readiness: list[ReadinessItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ProjectActiveIn(BaseModel):
    active: bool


# ----------------------------------------------------------------------------- runtimes
#
# Operator-owned substrates a repo's `process.yaml` may only REFERENCE by slug (design.md §7.5's
# trust direction; `factorial.runtimes` for the vocabulary).


class RuntimeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=SLUG_RE.pattern)
    name: str = Field(min_length=1)
    type: RuntimeType
    #: Validated per type by the route (`parse_runtime_details`); empty for every type today.
    details: dict[str, Any] = Field(default_factory=dict)
    #: Operator notes; the route checks the shape (`parse_runtime_metadata`) so the 422 can name
    #: the offending key the way `details` does.
    metadata: dict[str, Any] = Field(default_factory=dict)
    #: Keys stored with the runtime, by credential name; write-only like the credential routes.
    credentials: dict[str, SecretStr] = Field(default_factory=dict)


class RuntimeCheckIn(BaseModel):
    """Keys to test before a runtime exists; nothing in it is stored."""

    model_config = ConfigDict(extra="forbid")

    type: RuntimeType
    details: dict[str, Any] = Field(default_factory=dict)
    credentials: dict[str, SecretStr] = Field(default_factory=dict)


class RuntimeOptionsOut(_Out):
    #: `<runtime.type>@<details.isolation>` entries this deployment offers, sorted.
    offered: list[str]


class RuntimePatch(BaseModel):
    """`slug` and `type` are immutable — repos pin the first, credentials are shaped by the
    second — so a body naming either is a 422, never a silent no-op."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1)
    details: dict[str, Any] | None = None
    #: A present value replaces the whole map, as `details` does.
    metadata: dict[str, Any] | None = None


class RuntimeCredentialOut(_Out):
    """What the page may show of a stored credential: its name and the row's own clocks.
    `updated_at` moves on a replace (the 0011 trigger); `created_at` never does."""

    name: str
    created_at: datetime
    updated_at: datetime


class AuthFailureOut(_Out):
    """The newest run on a runtime that failed or idled on a refused credential."""

    at: datetime
    #: The run's `failure_reason`, or the idle turn's error, that matched.
    reason: str
    #: `<owner/repo>#<issue>`.
    ticket: str
    ticket_id: int


class CredentialCheckOut(_Out):
    ok: bool
    #: A fixed sentence: never the vendor's body, which can echo the credential.
    detail: str


class CredentialTestOut(CredentialCheckOut):
    #: `codex_subscription`: the stored login's expiry was read, and OpenAI was not asked.
    local: bool
    #: An E2B runtime's team key, checked beside the vendor credential.
    e2b: CredentialCheckOut | None = None


class RuntimeOut(_Out):
    id: int
    #: The owning organization's slug; `slug` is unique within it only.
    organization: str
    slug: str
    name: str
    type: str
    details: dict[str, Any]
    metadata: dict[str, str]
    #: Names only, like `ProjectDetail.secret_names`; there is no read-back for values.
    credential_names: list[str]
    #: The same rows with their timestamps, ordered by name like `credential_names`.
    credentials: list[RuntimeCredentialOut]
    #: Projects whose latest process version names this slug (`api.bindings`); the same rule
    #: the projects list's Runtime column and `GET /runtimes/{id}/projects` read.
    connected_projects: int
    last_auth_failure: AuthFailureOut | None = None
    created_at: datetime
    updated_at: datetime


class RuntimeTemplateBuildOut(_Out):
    """One ready template build in an E2B runtime's team: a pin the runtime may boot."""

    pin: str
    template: str
    build_id: str
    created_at: datetime
    cpu_count: int
    memory_mb: int
    #: Whether the template's name ends in the runtime's harness and a CLI version
    #: (`claude-2-1-310`), the name the builder gives it — the likely pick, never a rule.
    matches_harness: bool
    #: Whether it passed the session smoke; the worker refuses every other build (decision 25).
    verified: bool


class RuntimeCheckOut(_Out):
    model: CredentialCheckOut
    local: bool
    e2b: CredentialCheckOut | None = None
    #: The sandbox team's ready builds; empty unless the team key passed its check.
    templates: list[RuntimeTemplateBuildOut]


class RuntimeProjectOut(_Out):
    """One project bound to a runtime — enough to link to it and to its repository."""

    id: int
    name: str
    github_repo: str
    #: Where the repository link opens.
    forge: ForgeDisplayOut | None = None


class RuntimeCredentialIn(BaseModel):
    """Write-only, like `SecretValueIn`: encrypted on arrival, never read back over the API."""

    value: str = Field(min_length=1)


class RuntimeCredentialName(BaseModel):
    name: str


class RuntimeCredentialList(BaseModel):
    names: list[str]


class RuntimeLoginOut(BaseModel):
    """A started device-auth login: what the operator must open and type. The code is handed to
    the client here and recorded nowhere else."""

    session_id: str
    verification_url: str
    user_code: str
    expires_at: datetime


class RuntimeLoginStatusOut(BaseModel):
    session_id: str
    status: Literal["pending", "complete", "failed", "expired"]
    #: Why, for `failed` — the CLI's last lines. `None` otherwise.
    detail: str | None = None


# --------------------------------------------------------------------------- mcp tokens


McpTokenState = Literal["active", "expired", "revoked", "rotated"]


class McpMinterOut(_Out):
    """Who the dashboard would mint as. A session wins over a bearer sent beside it."""

    email: str | None
    via: Literal["session", "api_token"]
    #: The session's sign-in is over 24 hours old: minting refuses it until the person signs in
    #: again.
    reauth_required: bool


class McpTokenOut(_Out):
    """One minted MCP token, never its jti: that is shown once, inside the token."""

    id: int
    label: str
    access: Literal["read", "write"]
    created_by: str
    minted_via: Literal["session", "api_token"]
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    last_used_at: datetime | None = None
    last_used_client: str | None = None
    last_used_ip: str | None = None
    #: `rotated`: signed under an API token since replaced, so it verifies nowhere.
    state: McpTokenState


class McpTokenIndex(_Out):
    minter: McpMinterOut
    mcp_url: str
    #: Every token anyone minted, newest first. There are no roles (decision 12), so anyone signed
    #: in may revoke any of them.
    tokens: list[McpTokenOut]


class McpTokenMintIn(BaseModel):
    """Names no owner: the row records the session's email, or `api-token` for the shared
    bearer, which names no one."""

    label: str = Field(min_length=1, max_length=80)
    access: Literal["read", "write"]

    @field_validator("label")
    @classmethod
    def _printable_label(cls, value: str) -> str:
        # Format characters too: a bidi override or a zero-width space makes the token list
        # show one label and store another.
        if not value.strip() or not value.isprintable():
            raise ValueError("The label must be visible text with no control or format characters.")
        return value


class McpTokenMinted(BaseModel):
    """The token, this once. Nothing keeps it: the row holds only its jti's sha256."""

    token: str
    row: McpTokenOut


# ------------------------------------------------------------------------------ mcp tools


class McpToolOut(_Out):
    name: str
    title: str | None
    description: str
    #: A read-only token sees only the `factorial:read` tools.
    scope: Literal["factorial:read", "factorial:write"]
    destructive: bool


class McpToolCatalog(_Out):
    """Every tool the MCP server offers, whatever token asks: the dashboard lists them before
    anyone holds a token."""

    tools: list[McpToolOut]


class AssistantCredentialOut(_Out):
    """Whether a credential is stored, never the credential."""

    set: bool
    updated_by: str | None
    updated_at: datetime | None


class AssistantBackendOut(_Out):
    kind: str
    label: str
    #: The sign-ins the page offers: the deployment's offered ones, plus the saved one.
    auth_modes: list[str]
    #: auth mode -> the model the settings page switches to with it.
    default_models: dict[str, str]


class AssistantStoredCredentialOut(_Out):
    """One stored credential's place and provenance, never the credential."""

    backend: str
    auth_mode: str
    updated_by: str
    updated_at: datetime


class AssistantSettingsOut(_Out):
    settings: AssistantSettings
    #: Every stored credential, the saved backend and auth mode's among them, so one left behind
    #: by a switch can be seen and removed.
    credentials: list[AssistantStoredCredentialOut]
    backends: list[AssistantBackendOut]
    #: `<backend>:<auth_mode>` entries the deployment offers (`FACTORIAL_ASSISTANT__OFFERED`); the
    #: saved one may be missing, and the page then marks it no longer offered.
    offered: list[str]
    #: Same gate as sending a message: enabled, and a stored credential or a Bedrock mint.
    ready_to_send: bool
    #: The `assistant_status` row as the organization's saved backend sees it
    #: (`assistant_settings.backend_status`), written by the assistant process at start; null
    #: before then.
    status: dict[str, Any] | None


class AssistantReadyOut(_Out):
    """What the chat needs to say the assistant is not set up, for a caller who cannot read the
    settings themselves. `credential_updated_at` changes when the credential does, so a chat
    holding a refusal sees the fix even when the other fields read the same."""

    enabled: bool
    credential_set: bool
    credential_updated_at: datetime | None
    ready_to_send: bool
    #: The sentence a send would be refused with (`assistant_not_ready`), null when it would go.
    refusal: str | None


class AssistantCredentialIn(BaseModel):
    #: A Codex `auth.json` holds two JWTs and a refresh token, about 4 KB and more for some
    #: accounts.
    value: str = Field(min_length=1, max_length=16384)


# ---------------------------------------------------------------------------- assistant chat


class ChatConversationIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class ChatConversationPatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    archived: bool | None = None


class ChatConversationOut(_Out):
    id: int
    title: str | None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


class ChatItemOut(_Out):
    """One thing the Drawer draws. `rev` moves on every write, so the latest payload of an item
    is the one with the highest `rev`; it is also the stream's event id."""

    id: int
    turn_id: int | None
    kind: str
    payload: dict[str, Any]
    final: bool
    rev: int
    created_at: datetime


class ChatTurnOut(_Out):
    id: int
    status: str
    stop_reason: str | None
    failure_reason: str | None
    failure_message: str | None
    cost_usd: Decimal | None
    cost_source: str | None


class ChatItemsOut(_Out):
    items: list[ChatItemOut]
    #: The conversation's live turn, or else its last one.
    turn: ChatTurnOut | None


class GitHubTokenOut(_Out):
    """How the assistant process reads `repos` (design.md §7.18): a read-only token minted for
    one App and owner, or, for a token project, no token and the API's read proxy as
    `base_url`. A GitLab project's entry is always that proxy, and names the project and the
    bot login its tools read, which the process has no other way to learn."""

    repos: list[str]
    token: str
    expires_at: datetime
    base_url: str
    forge: Literal["github", "gitlab"] = "github"
    project_id: int | None = None
    bot_login: str = ""


class TurnCredentialIn(BaseModel):
    """The backend and auth mode the assistant's turn will run."""

    backend: str
    auth_mode: str


class TurnCredentialOut(BaseModel):
    credential: str


class ChatMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    #: The dashboard route the message was sent from. A path that is not a dashboard page is
    #: kept on the item but never reaches the model.
    page: str | None = Field(default=None, max_length=512)


class ChatMessageAccepted(_Out):
    turn_id: int


class AssistantHealthOut(_Out):
    ok: bool
    message: str | None
    #: When the assistant process last ran its check: an old one may be a process long gone.
    at: datetime | None


class ReadyResponse(HealthResponse):
    #: The assistant process's preflight, as it last started; null when it never has.
    assistant: AssistantHealthOut | None
