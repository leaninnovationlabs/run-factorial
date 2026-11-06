/**
 * The wire models, mirroring `orchestrator/api/schemas.py`.
 *
 * Hand-written rather than generated from the OpenAPI document: the surface is small, and a
 * generated client would hide exactly the shapes worth reading. Keys are snake_case because the
 * API's are — re-casing at the boundary would mean every field appears under two names depending
 * on which side of the fetch you are reading.
 */

/** `db.models.TicketStatus` — the pipeline status, of which labels are a projection. */
export type TicketStatus = "running" | "waiting_gate" | "blocked" | "done" | "exited";

/**
 * `db.models.StepRunStatus`. `approved` and `rejected` are human-gate outcomes.
 *
 * `interrupted` is a run a human stopped and means to continue — deliberately not `cancelled`,
 * because the orchestrator parks the ticket instead of retrying it.
 *
 * `idle` is a run whose agent is waiting on a person in its terminal: the session is
 * still live, so it can be joined, answered or interrupted.
 */
export type StepRunStatus =
  | "running"
  | "idle"
  | "waiting"
  | "succeeded"
  | "failed"
  | "timeout"
  | "cancelled"
  | "interrupted"
  | "approved"
  | "rejected";

export type StepKind = "agent" | "human_gate" | "action" | "check";

/** An organization (`GET /api/orgs`): the tenant every page under `/orgs/<slug>` acts in. The
 *  slug is the URL's and never changes; the name can. */
export interface Organization {
  slug: string;
  name: string;
  project_count: number;
  runtime_count: number;
  updated_by: string;
  created_at: string;
  updated_at: string;
  /** The reader's role in it; null for the operator's token, and while multi-org is off. */
  role: string | null;
}

/** One organization with what the reader may do in it (`GET /api/orgs/{org}`): the dashboard
 *  hides what the API would refuse. Every permission for the operator. `settings` is null for a
 *  reader without `org:manage`. */
export interface OrganizationDetail extends Organization {
  settings: Record<string, unknown> | null;
  permissions: string[];
}

/** A built-in role and the `module:action` permissions it holds (`GET /api/roles`). */
export interface Role {
  key: string;
  name: string;
  description: string;
  permissions: string[];
}

/** One key of the closed permission catalogue, in its order (`GET /api/permissions`). */
export interface Permission {
  key: string;
  description: string;
}

export interface Member {
  user_id: number;
  email: string;
  name: string;
  role: string;
  /** False for an account only a password signs in to: nothing proved it owns its email. */
  verified: boolean;
  added_by: string;
  created_at: string;
}

export interface Invite {
  id: number;
  email: string;
  role: string;
  created_by: string;
  created_at: string;
  expires_at: string;
}

/** The answer that creates an invite, the only one that holds its link. `url` is null where the
 *  deployment names no external URL; `path` is then joined to the page's own origin. */
export interface InviteCreated extends Invite {
  path: string;
  url: string | null;
}

export interface InvitePreview {
  organization_slug: string;
  organization_name: string;
  role: string;
  created_by: string;
  expires_at: string;
  /** Whether the signed-in account is the one the invite names. */
  for_you: boolean;
}

export interface Account {
  email: string;
  name: string;
}

export interface ForgeDisplay {
  kind: string;
  name: string;
  origin: string;
  pull_noun: string;
  issue_path: string;
  pull_path: string;
  tree_path: string;
  commit_path: string;
}

export interface Project {
  id: number;
  name: string;
  github_repo: string;
  description: string | null;
  settings: Record<string, unknown>;
  active: boolean;
  created_at: string;
  forge?: ForgeDisplay | null;
}

/** The runtime a project's latest process version names. `name` is null when no Runtime row
 *  carries the slug any more: the list still shows what the process says, unlinked. */
export interface ProjectRuntimeRef {
  slug: string;
  name: string | null;
}

/** `GET /api/orgs/{org}/projects`: a project plus its bound runtime, the one column the list draws that
 *  the detail read does not carry. */
export interface ProjectListItem extends Project {
  runtime: ProjectRuntimeRef | null;
  /** Absent on a payload from before the list carried it; that reads as GitHub. */
  ticket_source?: TicketSource | null;
}

export interface TicketSource {
  id: number;
  project_id: number;
  kind: string;
  external_ref: string;
  webhook_secret_ref: string | null;
  last_polled_at: string | null;
  created_at: string;
}

export interface ProcessVersion {
  id: number;
  project_id: number;
  /** Which lane this snapshot belongs to — the config's own `name:`. */
  process_key: string;
  /** The repository this snapshot was read from. One that is not the project's own is a version
   *  the project no longer offers, and its files are still read from here. */
  source_repo: string;
  /** The commit this snapshot was read from, and its identity. Routes still address a row by
   *  `id`; this is what a reader is shown, because it is the thing that can be looked up. */
  source_commit_sha: string;
  /** When the last sync stopped finding this lane's file, or a sync of another repository ran. A
   *  timestamp means retired, and the rows stay so history is readable. Null is offered only while
   *  `source_repo` is the project's repository. */
  retired_at: string | null;
  /** The ref the sync asked for. A sync with no ref reads the `factorial` branch and records it
   *  here; null means the default branch, which only rows synced before #248 say. */
  source_ref: string | null;
  /** Who adopted this snapshot's commit, when it was a direct push an operator vouched for. */
  adopted_by: string | null;
  /** When a sync last landed on this snapshot. The newest per lane is current. */
  activated_at: string;
  created_at: string;
}

/** One available lane, as the Process config page lists it (`api.schemas.ProcessSummary`). */
export interface ProcessSummary {
  process_key: string;
  /** The lane an issue carrying no process label enters. */
  is_default: boolean;
  /** The exact label that selects this lane, so it can be copied rather than retyped. */
  selection_label: string;
  latest: ProcessVersion;
}

/** What one sync did to one lane. `unchanged` is the outcome that makes the route usable with
 *  several lanes: a commit touching one leaves every other at a commit the project already has. */
export interface ProcessSyncResult {
  process: string;
  outcome: "created" | "unchanged" | "retired";
  /** The row to address in the snapshot routes; null for `retired`. The commit is the call's own
   *  `sha`, which every non-retired row shares. */
  version_id: number | null;
}

export interface ProcessSync {
  sha: string;
  results: ProcessSyncResult[];
  /** One line per referenced agent that does not resolve at the synced sha, deduplicated across
   *  lanes. Empty means sound. */
  agent_warnings: string[];
  /** Set when the commit retired the lane the project defaulted to, so the sync moved the
   *  default to one the commit offers. Null when the default did not move. */
  default_process_moved: DefaultProcessMoved | null;
}

/** Both ends of a default the sync had to re-point. The old lane is retired by the same commit,
 *  which is why the default could not simply be left alone. */
export interface DefaultProcessMoved {
  previous: string;
  now: string;
}

/** One commit between the last known commit and the head. `author_name` is what the pusher's
 *  git config said, which an agent sets freely: show it as claimed. */
export interface ComparedCommit {
  sha: string;
  message: string;
  author_name: string | null;
  authored_at: string | null;
}

export interface ComparedFile {
  path: string;
  status: string;
  additions: number;
  deletions: number;
  previous_path: string | null;
}

/** What adopting the `factorial` head would vouch for. */
export interface UnknownHead {
  head_sha: string;
  /** Null when no factory commit sits under the head. */
  known_base: string | null;
  commits: ComparedCommit[];
  files: ComparedFile[];
  total_commits: number;
  files_truncated: boolean;
  compare_url: string;
}

export interface ConfigAdoption {
  sha: string;
  /** False when the head was already known: nothing was recorded, and it was synced again. */
  adopted: boolean;
  sync: ProcessSync | null;
  sync_error: string | null;
}

/** One open issue the admission pass declines, and why. Recomputed per request rather than read
 *  back from the event log, which can say what happened but not whether it still is. */
export interface PendingIssue {
  external_key: string;
  title: string;
  /** The lane the issue selected, when selection got that far. Null for a selection refusal. */
  process_key: string | null;
  /** `not_ready` is the ordinary state of an issue nobody has marked for the factory; the other
   *  three are misconfiguration. */
  reason: "no_default" | "unknown_process" | "ambiguous_process" | "not_ready";
  detail: string;
  /** The leaves of the lane's `entry.when` that are still false, one line each as the evaluator
   *  writes them. Empty for the three refusals: no lane resolved, so nothing was evaluated. */
  awaiting: string[];
  /** The same leaves in words (`predicates/words.py::needs`). Optional: for a moment during a
   *  deploy the dashboard can be newer than the API it talks to, which then sends only
   *  `awaiting`. */
  needs?: string[];
}

export interface RuntimeProvidedSecret {
  name: string;
  runtime_slug: string;
  /** False is the runtime readiness row's blocker: the runtime holds no key of its type. A host-minted type reports true without a stored row. */
  stored: boolean;
  /** Open tickets pinned to a snapshot that still reads the project copy — config is pinned
   *  per ticket, so the latest version naming the runtime does not retire the copy for them. */
  open_tickets_reading_copy: number;
}

/** How a run is supplied a project secret (decision 75): the agent's environment, or a header a
 *  cloud sandbox's egress proxy sets on one host. */
export type SandboxDelivery = "header" | "env";

export interface SecretDelivery {
  name: string;
  mode: SandboxDelivery;
  host: string | null;
  header: string | null;
}

/** How a run is supplied a secret, as a write or a change sends it. */
export interface SecretDeliveryWrite {
  sandbox_delivery?: SandboxDelivery;
  egress_host?: string;
  egress_header?: string;
}

/** A secret write: the value, and how a run is supplied it. Replaces the whole row. */
export interface SecretWrite extends SecretDeliveryWrite {
  value: string;
}

export interface ProjectDetail extends Project {
  ticket_source: TicketSource | null;
  /** The repo-side rows `GET /readiness` will return, so the page holds their places from its
   *  first paint. */
  probe_keys?: ReadinessKey[];
  /** Every lane a ticket could be admitted into right now. Retired lanes are absent. */
  processes: ProcessSummary[];
  /** The lane an issue carrying no process label enters. Null means every issue must name one,
   *  which is allowed. */
  default_process: string | null;
  ticket_count: number;
  open_ticket_count: number;
  exited_ticket_count: number;
  secret_names: string[];
  /** Every key the pinned manifests declare, `GH_TOKEN` included — the Secrets section's rows. */
  declared_secrets: string[];
  /** The vendor key the process's runtime owns outright (decision 36): the project store is
   *  never read for it, so the section labels the row instead of asking for a value. */
  runtime_provided_secrets: RuntimeProvidedSecret[];
  secret_deliveries: SecretDelivery[];
  /** Empty means the project can be activated. The server computes this the same way it
   *  enforces it, so the Activate button and the API can never disagree. */
  blockers: string[];
  /** Advisory, best-effort readiness facts ("not checked" notes when a pinned manifest could
   *  not be read). Never gates activation — render, don't disable. */
  warnings: string[];
  /** The same facts as `blockers` + `warnings`, one row per requirement including the passing
   *  ones, addressed by `key` rather than by position. `blockers` is a projection of these.
   *  The repo-side rows are NOT here — they cost a live GitHub call each, so they answer on
   *  `ProjectReadiness` and this page does not wait for them. */
  readiness: ReadinessItem[];
}

/** The readiness rows whose verdict is a live GitHub read (`GET /projects/{id}/readiness`).
 *
 *  None of them can ever be `blocked` — a live call must not be able to refuse an activation —
 *  so nothing on the page waits for this: the overview, or a probed section's page, asks for it
 *  and fills in. */
export interface ProjectReadiness {
  readiness: ReadinessItem[];
  warnings: string[];
}

export type ComponentKind = "agent" | "skill" | "mcp";

/** One file the catalog could contribute, with everything the explorer needs to draw it
 *  (`api.schemas.FileProposalOut`). */
export interface FileProposal {
  path: string;
  body: string;
  component: string;
  kind: ComponentKind;
  variant: string | null;
  version: number;
  /** The markers this file used and their values. The client compares them after a config edit
   *  to decide whether a body it has edited has gone stale
   *  (`components/process/explorer/bundleDraft.ts`'s `reconcile`); the server never trusts the
   *  copy that comes back, because it recomputes the proposal at submit anyway. */
  facts: Record<string, string>;
  /** Already in the target repo. Shown, locked, and never written — the never-overwrite rule
   *  made visible, because an unexplained absence reads as a bug. */
  present_in_repo: boolean;
}

export interface VariantOffer {
  variant: string;
  summary: string;
  offered: boolean;
  default_on: boolean;
  reason: string | null;
  paths: string[];
}

export interface ComponentProposal {
  name: string;
  kind: ComponentKind;
  variants: VariantOffer[];
  present_in_repo: boolean;
  /** The config names this component. A shared skill or MCP server is here because a manifest
   *  declares it, which must read differently. */
  referenced: boolean;
}

/** What a commit from this content would create under `.factorial/`, before the operator commits to it.
 *  Reads the repo; writes nothing (`POST /projects/:id/config-preview`). */
export interface ConfigPreview {
  files: FileProposal[];
  components: ComponentProposal[];
}

/** One file the operator assembled into the bundle (`api.schemas.SelectedFileIn`). */
export interface SelectedFile {
  path: string;
  body: string;
}

/** A file the `factorial` branch already has, rewritten by the operator (`api.schemas.ConfigEditIn`). */
export interface ConfigEdit {
  path: string;
  body: string;
  /** The git blob the edit started from. */
  base_sha: string;
}

export interface ConfigCommitResult {
  sha: string;
  /** The commit on GitHub. */
  url: string;
  branch: string;
  /** False when the branch already held the draft: nothing was written, and `sha` is the head
   *  that was synced again. */
  committed: boolean;
  components: ComponentProposal[];
  /** Every path the commit wrote, stubs included. */
  written: string[];
  /** Null when the sync failed after the commit landed; `sync_error` then says why. */
  sync: ProcessSync | null;
  sync_error: string | null;
}

/** The branch `POST /config-branch` seeded: a copy of the default branch's config folder, or
 *  only a README for an operator starting from a template. */
export interface ConfigBranchResult {
  branch: string;
  sha: string;
}

export type SeedSource = "default_branch" | "empty";

/** One lane of a config the gallery previews, in a version snapshot's shape. */
export interface LanePreview {
  key: string;
  content: Record<string, unknown>;
  agents: AgentDetail[];
}

/** What a seed from the default branch would copy (`GET /default-branch-config`). */
export interface DefaultBranchConfig {
  branch: string;
  sha: string;
  config_dir: string;
  /** Empty when `error` is set. */
  lanes: LanePreview[];
  /** Why the folder does not parse. Copying is still allowed; the editor fixes it. */
  error: string | null;
}

export type ReadinessKey =
  | "app_access"
  | "github_connection"
  | "github_webhook"
  | "config_branch"
  | "config_branch_rules"
  | "ticket_source"
  | "process_config"
  | "secrets"
  | "labels"
  | "branch_protection"
  | "ci_workflows"
  | "gate_approvers"
  | "runtime";

/** A project's GitHub credentials as the API reads them back: whether each secret is stored, never
 *  its value. `mode` is null before one is chosen. */
export interface GitHubConnection {
  mode: "app" | "token" | null;
  app_id: string;
  bot_login: string;
  installations: Record<string, number>;
  private_key_set: boolean;
  webhook_secret_set: boolean;
  token_set: boolean;
  /** Where GitHub is told to deliver this project's webhooks; empty before a mode is chosen. */
  webhook_url: string;
}

/** A blank secret keeps the stored one, so a field the operator did not type is left out. */
export interface GitHubConnectionIn {
  mode: "app" | "token";
  app_id?: string;
  private_key?: string;
  webhook_secret?: string;
  token?: string;
  bot_login?: string;
}

/** One configuration requirement's verdict.
 *
 *  `unknown` is not a soft `blocked`: every GitHub-backed probe degrades rather than raising, so
 *  an unconfigured App or a timeout resolves here. Rendering it as a cross would send someone to
 *  fix working configuration, so it gets its own neutral treatment. */
export interface ReadinessItem {
  key: ReadinessKey;
  /** Which lane this row is about, when the requirement is lane-specific. Only `runtime` sets
   *  one, so rows are identified by `key` + `process`, never by `key` alone. */
  process: string | null;
  /** The slug that process names. Only `runtime` rows set one. */
  runtime_slug: string | null;
  state: "ok" | "blocked" | "warn" | "unknown";
  summary: string;
  detail: string | null;
  /** A shell command to copy, for rows whose fix is outside the App's granted permissions. */
  remedy: string | null;
  /** A GitHub settings page, when the fix lives there rather than in a command. Only
   *  `app_access` sets one — and only an account owner can open it. */
  link: string | null;
  /** The rules this row is the verdict of, one by one. Only the two branch rows carry any, and
   *  only once their read succeeded; the rows the pages make up themselves leave it out. */
  checks?: ReadinessCheck[];
}

/** One setting a branch row reads, with the fix for it alone when it fails. */
export interface ReadinessCheck {
  key: string;
  title: string;
  description: string;
  passed: boolean;
  /** A shell command that changes this setting and no other. */
  remedy: string | null;
  /** A GitHub settings page, for a fix no command can make safely. */
  link: string | null;
}

/** Why a run was parked (`runtime.contract.ParkReason`): a human's Interrupt, a
 *  session left idle (decision 56), a session ended from its terminal, a token rollover that
 *  resumes on the same tick, or a session that reached `terminal.session_ceiling`. */
export type ParkReason = "user" | "idle" | "exited" | "rollover" | "ceiling";

export interface TicketListItem {
  id: number;
  external_key: string;
  /** The issue title as the orchestrator last observed it, or null for a ticket it has not ticked
   *  since titles were stored. Rows fall back to the issue number. */
  title: string | null;
  current_step: string;
  status: TicketStatus;
  /** The status of the highest attempt at `current_step`, or null when it has none. See
   *  `OverviewTicket.latest_run_status` — the same refinement, for the project page's rows. */
  latest_run_status: StepRunStatus | null;
  /** While that run is parked, why; null otherwise. `idle` is Waiting, every other reason
   *  Interrupted (`ui/src/status.ts`). */
  park_reason: ParkReason | null;
  owner_at_admission: string;
  process_version_id: number;
  /** The lane this ticket runs, from the snapshot it pinned. Badged on the row: with several
   *  processes per project, a step name no longer says which process it belongs to. */
  process_key: string;
  attempts: number;
  /** The same three figures `StepRun` carries per attempt, summed over the ticket's. Zero rather
   *  than null for a ticket that has not run — see `ProjectSpend`. */
  input_tokens: number;
  output_tokens: number;
  /** Serialized as a string: `Decimal` over JSON, so it keeps its exact scale. */
  cost_usd: string;
  /** True when any summed run's figure is an estimate: one estimated part makes the total one. */
  cost_estimated: boolean;
  admitted_at: string;
  closed_at: string | null;
  /** See `TicketDetail.quarantined_at`. */
  quarantined_at: string | null;
  updated_at: string;
}

export interface TicketListResponse {
  project_id: number;
  status: TicketStatus | null;
  open_only: boolean;
  tickets: TicketListItem[];
}

export interface StepRun {
  id: number;
  step_name: string;
  kind: StepKind;
  attempt: number;
  status: StepRunStatus;
  failure_reason: string | null;
  decided_by: string | null;
  runtime_run_id: string | null;
  /**
   * The substrate this run's manifest DECLARES — not necessarily the one that executed it.
   * The executing substrate is carried by the `agent_spawned` timeline entry.
   */
  runtime_kind: string | null;
  /** The model id the run was specced with, stamped at spawn. Null for gates and actions. */
  model: string | null;
  input_tokens: number | null;
  /** The cache-read part of `input_tokens`, when the substrate reported one. */
  cached_input_tokens: number | null;
  output_tokens: number | null;
  /** Serialized as a string: `Decimal` over JSON, so it keeps its exact scale. */
  cost_usd: string | null;
  /** What kind of figure `cost_usd` is: measured by the substrate, or priced by the worker at
   *  list from the tokens. Null with `cost_usd` when neither could say. */
  cost_source: CostSource | null;
  /** Who forced the run past its failed conditions (decision 78). Its `status` then reads
   *  `succeeded`, which the page never shows as a success. */
  forced_by: string | null;
  /** When a redo set the run aside (decision 76): history, not where the ticket stands. */
  superseded_at: string | null;
  started_at: string | null;
  ended_at: string | null;
  created_at: string;
  /** The ticket's generation when the run started (`TicketDetail.generation`). Attempt numbers
   *  keep counting across a restart, so this, not the number, tells a run from before one. */
  generation: number;
}

/** A session waiting at its prompt with its postconditions unmet. */
export interface TerminalIdle {
  reason: string;
  since: string | null;
  /** The agent's question: agent-written text, shown as text. */
  question: string | null;
  /** When the loop acts at the earliest if nobody answers. Typing into the terminal defers
   * it, and a retry also waits for the step's CI to settle. */
  parks_at: string | null;
  /** What the loop does then: park for a person, or end the run so its step retries. */
  at_deadline: "park" | "retry";
}

/** Why a parked session stopped: `idle`, `exited` (its terminal was quit), `rollover`,
 *  `ceiling`, or `user`. */
export interface TerminalParked {
  reason: string;
  question: string | null;
  /** The error the stretch's last turn ended in: data, shown as text. */
  turn_error: string | null;
  /** The postconditions still unmet when it went idle, or when its terminal was quit. */
  unmet: string[] | null;
  /** `unmet` in words. Absent from an API older than it, and null where `unmet` is or where the
   *  park's event predates it. */
  needs?: string[] | null;
  at: string;
}

/** The run's open session (the inspect view): `starting` while a request waits for the inspect pass, `failed`
 *  when that pass refused it or the start failed. */
export interface InspectInfo {
  status: "none" | "starting" | "open" | "parked" | "failed";
  reason: string | null;
  /** What `reason`'s code is worded with (`viewClosed`): who closed it, a limit, a span. */
  reason_details?: Record<string, string | number>;
  startable: boolean;
  unavailable: string | null;
}

/** `GET /step-runs/{id}/terminal` (decision 57): what the run's terminal offers, by the rule the
 *  gateway admits by. */
export interface TerminalInfo {
  step_run_id: number;
  enabled: boolean;
  /** The origin the gateway answers on; empty for the API's own (decision 57). */
  gateway_origin: string;
  kind: "session" | "inspect" | null;
  attachable: boolean;
  /** Why the gateway would not accept a viewer now, as its close frame says it. */
  why_not: string | null;
  why_not_code: number | null;
  /** Why a person cannot type into it, when the gateway would accept a viewer. */
  read_only: string | null;
  status: string;
  idle: TerminalIdle | null;
  parked: TerminalParked | null;
  resumable: boolean;
  resume_pending: boolean;
  inspect: InspectInfo;
  kept_until: string | null;
  /** The answer cannot change until someone acts. */
  settled: boolean;
  /** The earliest later run working in this run's workspace, and its attempt number. */
  continued_by: number | null;
  continued_by_attempt: number | null;
}

/** `reported` is a bill; `estimated` is a list price the worker computed — notional on a
 *  subscription — and is drawn with a `~` wherever it is shown. */
export type CostSource = "reported" | "estimated";

/** A snapshot, by its row and by the commit it was read from (`api.schemas.PinOut`). */
export interface Pin {
  process_version_id: number;
  sha: string;
}

export interface TicketDetail {
  id: number;
  project_id: number;
  external_key: string;
  /** The observed issue title, as the lists carry it. Null for a ticket the orchestrator has not
   *  ticked since titles were stored; the header falls back to the issue number. */
  title: string | null;
  process_version_id: number;
  /** See `TicketListItem.process_key`. Read from the pinned snapshot, so a ticket keeps the lane
   *  it entered however the project's default or its own labels change later. */
  process_key: string;
  /** The commit the pinned snapshot was read from. */
  process_sha: string;
  /** The lane's current snapshot, or null when the lane is retired. Another `process_version_id`
   *  than the ticket's own is what offers Restart on latest (#321). */
  lane_head: Pin | null;
  /** 1 from admission, one more per restart. Only this generation's runs say where the ticket
   *  stands; earlier ones are history (`StepRun.generation`). */
  generation: number;
  owner_at_admission: string;
  current_step: string;
  status: TicketStatus;
  admitted_at: string;
  closed_at: string | null;
  /** Non-null means the loop has stopped leasing this ticket (decision 31) — see the requeue
   *  button in the header and condition `quarantined` in `status.ts`. */
  quarantined_at: string | null;
  /** Passes that raised in a row; non-zero is why refresh is refused. Optional: for a moment
   *  during a deploy the dashboard can be newer than the API it talks to. */
  consecutive_failures?: number;
  /** See `TicketListItem.park_reason`. */
  park_reason?: ParkReason | null;
  /** The step a Redo pressed now would start again, or null where Redo is not offered. */
  redo_target: string | null;
  /** The current step's preconditions from the pinned config, in words, with the ticket's own
   *  values filled in; empty when the step has none. Optional only for an API older than it. */
  waiting_on?: string[];
  step_runs: StepRun[];
}

/** Exit, re-enter, restart, interrupt, requeue: always 202, a request. Each waits for the pass it
 *  woke (decision 49), so `landed` says whether the refetch that follows will show that pass. */
export interface RequestResult {
  status: string;
  landed: boolean;
}

/** A run a restart would stop (`api.schemas.RestartRunOut`). */
export interface RestartRun {
  step: string;
  attempt: number;
  kind: string;
}

/** An open pull request a restart would close (`api.schemas.RestartPrOut`). */
export interface RestartPr {
  number: number;
  url: string;
  head: string;
  base: string;
}

/** Why a restart cannot go ahead now — `issue_closed`, `quarantined`, `done` or `lane_retired` —
 *  with the API's own sentence for it. */
export interface RestartBlocker {
  reason: string;
  detail: string;
}

/**
 * `GET /api/tickets/:id/restart/preview`: what a restart would cancel, abandon, close and delete,
 * read live off GitHub. A forecast only: the worker works it out again from its own observation
 * when it answers. `target` is null for a retired lane; `current_pin` is a ticket already on it.
 */
export interface RestartPreview {
  current: Pin;
  target: Pin | null;
  current_pin: boolean;
  /** The run in flight it cancels. */
  live_run: RestartRun | null;
  /** The interrupted or idle-parked session it abandons, with its kept workspace. */
  parked_run: RestartRun | null;
  close_prs: RestartPr[];
  delete_branches: string[];
  /** The ticket's pull requests already merged into a branch it does not own, such as `main`. A
   *  restart may repeat that work, so the person confirms it, and the request says
   *  `allow_merged`. */
  merged_outside: RestartPr[];
  blockers: RestartBlocker[];
}

/** `POST /refresh` (decision 4): always 202. */
export interface FleetRefreshResult {
  requested_at: string;
}

/** `POST /tickets/{id}/refresh` (decision 4): 200 when `landed`, 202 when the API stopped waiting
 *  first. Either way `ticket` is the ticket as it stands. */
export interface RefreshResult {
  /** A pass that claimed the ticket after the request has released it. */
  landed: boolean;
  requested_at: string;
  ticket: TicketDetail;
}

/**
 * The config snapshot a ticket pinned at admission (`GET /api/tickets/:id/process`).
 *
 * `content` is `dict[str, Any]` on the wire — an old or hand-posted snapshot may be missing
 * anything — so it stays unknown here and is narrowed by `isProcessConfigLike` before the
 * process graph reads it. Its keys are `process.yaml`'s camelCase, not the API's snake_case.
 */
export interface TicketProcess {
  ticket_id: number;
  process_version_id: number;
  /** Which lane this ticket runs, and the commit its config was read from — together, what
   *  identifies the steps for the rest of this ticket's life. */
  process_key: string;
  source_commit_sha: string;
  content: Record<string, unknown>;
}

/**
 * A ticket's issue, read live off GitHub (`GET /api/tickets/:id/issue`).
 *
 * Not part of `TicketDetail`: the orchestrator stores no copy of the issue text — GitHub owns it
 * and the loop only observes it — so this costs a round trip to GitHub and is fetched on demand,
 * when a reader actually opens the description.
 */
export interface TicketIssue {
  ticket_id: number;
  repo: string;
  number: number;
  title: string;
  /** The body verbatim, markdown and all. Empty string for an issue with no description. */
  body: string;
  /** `open` or `closed`, as GitHub spells it — not the ticket's pipeline status. */
  state: string;
}

/** Live gate pull/MR pick from `GET /tickets/{id}/gate-pull-request`. */
export interface GatePullRequest {
  ticket_id: number;
  number: number | null;
  url: string | null;
  head_ref: string | null;
  /** `closed` and not `merged` is a gate holding on a close (decision 79). */
  state: string | null;
  merged: boolean | null;
}

export interface TimelineEntry {
  /** The event's log id — total order, and the stable identity of this entry. */
  id: number;
  type: string;
  details: Record<string, unknown>;
  created_at: string;
}

export interface TimelineResponse {
  ticket_id: number;
  entries: TimelineEntry[];
}

export interface BoardCell {
  current_step: string;
  status: TicketStatus;
  count: number;
}

export interface InFlightRun {
  step_run_id: number;
  ticket_id: number;
  external_key: string;
  step_name: string;
  kind: StepKind;
  attempt: number;
  status: StepRunStatus;
  runtime_run_id: string | null;
  started_at: string | null;
  created_at: string;
}

/** A project's usage across every attempt of every ticket. Zero, never null: a project that has
 *  spent nothing has spent zero, and the header decides for itself what to draw for that. */
export interface ProjectSpend {
  input_tokens: number;
  output_tokens: number;
  /** Serialized as a string: `Decimal` over JSON, so it keeps its exact scale. */
  cost_usd: string;
  /** See `TicketListItem.cost_estimated`. */
  cost_estimated: boolean;
}

export interface BoardResponse {
  project_id: number;
  cells: BoardCell[];
  in_flight: InFlightRun[];
  /** Carried by the board rather than by `ProjectDetail` because this is the project read the
   *  dashboard polls; the detail read probes GitHub and is fetched once. */
  spend: ProjectSpend;
}

/** `?span=` on the dashboard read: whole UTC Monday weeks plus the current one, clipped to the
 *  first admission, or all time. Every figure follows it but `lifetime` and the NOW figures. */
export type Span = "4w" | "12w" | "all";

/** `db.models.EnteredBy`: how an attempt came to be. `reject_rerun` and `redo` are rework,
 *  `retry` follows a failure, and `restart` is a new generation's first attempt (#321); a row
 *  older than the column is null, and is none of them. */
export type EnteredBy = "next" | "reject_rerun" | "admission" | "retry" | "restart" | "redo";

/** `done`, `exited`, or else `open`: a complement, not a stored status. */
export type TicketOutcome = "done" | "open" | "exited";

/** `RunCountsOut`: how a set of runs was priced. Gates count; `subscription` counts runs on a
 *  `*_subscription` runtime, whose dollars are API-rate equivalents rather than an invoice. */
export interface RunCounts {
  total: number;
  priced: number;
  estimated: number;
  subscription: number;
  cached_input_tokens: number;
}

/** `PerDoneOut`: done tickets each priced over their whole life; the two figures are null until
 *  one is done. */
export interface PerDone {
  tickets: number;
  mean_usd: string | null;
  median_usd: string | null;
  cost_estimated: boolean;
}

/** Whatever the span: kept for MCP callers, and the Spend card's "all time" line. */
export interface DashboardLifetime {
  /** The board's figure, served again here so the cards and the bars under them are one read. */
  spend: ProjectSpend;
  tickets: { done: number; exited: number; open: number; total: number };
  per_done: PerDone;
  runs: RunCounts;
}

/** Distinct tickets with that closure in the period; `admitted` by `admitted_at`. */
export interface PeriodTickets {
  done: number;
  exited: number;
  admitted: number;
}

/** Admission to the latest done event, over the tickets done in the period. */
export interface LeadTime {
  tickets: number;
  median_seconds: number | null;
}

/** `PeriodFiguresOut`: the cards' figures over one period. */
export interface PeriodFigures {
  /** `WindowSpendOut`, the same four fields as the board's spend, over the period's runs. */
  spend: ProjectSpend;
  runs: RunCounts;
  tickets: PeriodTickets;
  /** Tickets done in the period, each priced over its whole life. */
  per_done: PerDone;
  /** Done per 7 days; null for a period under a day. */
  velocity_per_week: number | null;
  lead_time: LeadTime;
  /** Runs in the period a person forced past their failed conditions (decision 78). */
  forced_steps: number;
}

/** `TimeSplitOut`: the lead time of the tickets done in the window, each over its whole life.
 *  `idle_seconds` is what is left per ticket, never below zero. */
export interface TimeSplitFigures {
  tickets: number;
  lead_seconds: number;
  agent_seconds: number;
  gate_seconds: number;
  paused_seconds: number;
  idle_seconds: number;
}

/** `OpenGateOut`: a human gate waiting at `as_of`. NOW. */
export interface OpenGate {
  ticket_id: number;
  external_key: string;
  title: string | null;
  step: string;
  attempt: number;
  waiting_since: string;
}

/** `SlowestDoneOut`: the ticket done in the window with the longest lead time, and its single
 *  longest gate wait, null when it had none. */
export interface SlowestDone {
  ticket_id: number;
  external_key: string;
  title: string | null;
  lead_seconds: number;
  wait_step: string | null;
  wait_seconds: number | null;
}

export interface OutcomeSpend {
  outcome: TicketOutcome;
  tickets: number;
  cost_usd: string;
  tokens: number;
  cost_estimated: boolean;
}

export interface TicketSpend {
  ticket_id: number;
  external_key: string;
  title: string | null;
  status: TicketStatus;
  attempts: number;
  tokens: number;
  cost_usd: string;
  cost_estimated: boolean;
}

/** The tickets past the top of the list, folded into one row. */
export interface SpendFold {
  tickets: number;
  cost_usd: string;
  cost_estimated: boolean;
}

export interface StepSegment {
  step: string;
  kind: StepKind;
  attempt: number;
  entered_by: EnteredBy | null;
  status: StepRunStatus;
  model: string | null;
  runtime_kind: string | null;
  runtime_type: string | null;
  started_at: string | null;
  ended_at: string | null;
  /** Time the run sat paused for a person, inside `started_at` to `ended_at`. */
  parked_seconds: number;
  /** Active seconds: `ended_at - started_at - parked_seconds`; null while the run is going, or
   *  if it never started. */
  seconds: number | null;
  tokens: number;
  cost_usd: string;
  cost_estimated: boolean;
  /** `StepRun.forced_by`. */
  forced_by: string | null;
  /** `StepRun.superseded_at`. */
  superseded_at: string | null;
}

export interface TicketStepCosts {
  ticket_id: number;
  external_key: string;
  title: string | null;
  cost_usd: string;
  cost_estimated: boolean;
  /** In run order: by first start, then by row. */
  segments: StepSegment[];
}

/** `events.read_models.StepTime`: one step of one lane and its median run, or wait for a gate. */
export interface StepTime {
  process_key: string;
  step: string;
  kind: StepKind;
  runs: number;
  median_seconds: number;
  /** Null under `limits.p90_min_runs` runs. */
  p90_seconds: number | null;
  max_seconds: number;
}

export interface SpendBucket {
  count: number;
  cost_usd: string;
  cost_estimated: boolean;
}

/** Spend that produced no merged change. The rows overlap on purpose, and `total_usd` is over
 *  the union of their runs, so a failed attempt of an exited ticket counts once. */
export interface UnmergedSpend {
  rework: SpendBucket;
  exited: SpendBucket;
  failed: SpendBucket;
  /** Agent attempts a redo set aside, counted in attempts. */
  set_aside: SpendBucket;
  total_usd: string;
  cost_estimated: boolean;
}

export interface ClosedTicket {
  ticket_id: number;
  external_key: string;
  outcome: "done" | "exited";
}

export interface DashboardWeek {
  /** A UTC Monday, as a date: `2026-09-14`. */
  week_start: string;
  done: number;
  exited: number;
  cost_usd: string;
  cost_estimated: boolean;
  closed: ClosedTicket[];
}

export interface LargestRun {
  ticket_id: number;
  external_key: string;
  title: string | null;
  step: string;
  attempt: number;
  model: string | null;
  cost_usd: string;
  cost_estimated: boolean;
  tokens: number;
  seconds: number | null;
}

export interface MostAttempts {
  ticket_id: number;
  external_key: string;
  title: string | null;
  step: string;
  attempts: number;
  rework: number;
  retries: number;
  cost_usd: string;
  rework_cost_usd: string;
  cost_estimated: boolean;
}

export interface LongestGate {
  ticket_id: number;
  external_key: string;
  title: string | null;
  step: string;
  attempt: number;
  status: StepRunStatus;
  seconds: number;
}

/** The callouts, by rule; each is null when its rule finds nothing. */
export interface DashboardExtremes {
  largest_run: LargestRun | null;
  most_attempts: MostAttempts | null;
  /** Decided or cancelled, so a wait cut short by an exit still shows. */
  longest_gate: LongestGate | null;
  slowest_done: SlowestDone | null;
  /** The first of `open_gates`. NOW. */
  longest_open_wait: OpenGate | null;
}

/** The server's list bounds and thresholds, served back. */
export interface DashboardLimits {
  by_ticket: number;
  step_costs: number;
  open_gates: number;
  p90_min_runs: number;
}

/**
 * The project dashboard's one read, `GET /api/projects/{id}/dashboard` (tech-stack decision 61).
 * Every figure the tab draws is in here; the browser computes nothing from it but a bar's width.
 * Every sum carries `cost_estimated`; every duration is in seconds.
 */
export interface ProjectDashboard {
  project_id: number;
  span: Span;
  /** The window's start: a UTC Monday `span` weeks before this week's, moved up to the first
   *  admission when that is later (`clipped`); the first admission for `all`. */
  since: string;
  /** The start of the period of the same length before `since`, or null when there is none. */
  previous_since: string | null;
  clipped: boolean;
  /** One clock read at the top of the request, for "as of" and the window's end. */
  as_of: string;
  /** Whatever the span. The cards read `period`. */
  lifetime: DashboardLifetime;
  period: PeriodFigures;
  /** Null for `all`, for a clipped window, and when the period before starts before the first
   *  admission. */
  previous: PeriodFigures | null;
  /** Tickets neither done nor exited at `as_of`. NOW. */
  open_now: number;
  time_split: TimeSplitFigures;
  /** Longest waiting first, cut at `limits.open_gates`. NOW. */
  open_gates: OpenGate[];
  /** Always three rows, `done`, `open`, `exited`, zero-filled. */
  by_outcome: OutcomeSpend[];
  /** The costliest tickets, costliest first; `limits.by_ticket` of them. */
  by_ticket: TicketSpend[];
  by_ticket_rest: SpendFold | null;
  /** Admitted tickets with no run at all; they count under `open` at zero. */
  unrun_tickets: number;
  /** Per-attempt costs for the costliest `limits.step_costs` tickets. */
  step_costs: TicketStepCosts[];
  /** Agent, check and action steps, then the gates, each ordered by first start. */
  steps: StepTime[];
  gates: StepTime[];
  unmerged: UnmergedSpend;
  /** UTC Monday weeks, oldest first. */
  weeks: DashboardWeek[];
  extremes: DashboardExtremes;
  limits: DashboardLimits;
}

export interface OverviewProject {
  id: number;
  name: string;
  active: boolean;
}

/**
 * One ticket as the factory board draws it (`GET /api/orgs/{org}/overview`).
 */
export interface OverviewTicket {
  ticket_id: number;
  project_id: number;
  external_key: string;
  /** Observed issue title, or null for a ticket not ticked since the column shipped. The
   *  orchestrator's loop already reads the issue every tick, so this costs no extra round trip. */
  title: string | null;
  current_step: string;
  status: TicketStatus;
  /** Which ADLC lane this ticket runs. Badged per row and deliberately not a board axis: with
   *  several processes per project, step names are no longer stable even within one project,
   *  which is why the bands key on status. */
  process_key: string;
  /** The status of the highest attempt at `current_step`, or null when it has none.
   *  `status` alone cannot tell an interrupted ticket from an escalated one — both park as
   *  `blocked` — so the lane and the pill are derived from the pair (`ui/src/status.ts`). */
  latest_run_status: StepRunStatus | null;
  /** See `TicketListItem.park_reason`. */
  park_reason: ParkReason | null;
  /** `step_run` rows across every step — "has it run at all", not a retry count. */
  runs: number;
  /** Attempt at the step it is on now, counted from the ticket's last restart, or null between
   *  steps. Over 1 means Retrying. */
  attempt: number | null;
  /** Serialized as a string: `Decimal` over JSON, so it keeps its exact scale. */
  cost_usd: string;
  /** See `TicketListItem.cost_estimated`. */
  cost_estimated: boolean;
  tokens: number;
  minutes: number;
  /** See `TicketDetail.quarantined_at`. */
  quarantined_at: string | null;
}

export interface OverviewResponse {
  projects: OverviewProject[];
  tickets: OverviewTicket[];
  done_days: number;
}

// --- runtimes ---------------------------------------------------------------------------------

/**
 * `db.models.RuntimeType`. The family (`codex_*` / `claude_*`) decides which adapter executes and
 * which manifest `runtime.kind` a process may pair it with; the suffix decides how it signs in.
 * A subscription is connected by device login for codex and by a pasted `claude setup-token`
 * for claude, which has no device flow (the login route answers 409 for it).
 */
export type RuntimeType =
  | "codex_api"
  | "codex_subscription"
  | "codex_bedrock"
  | "claude_api"
  | "claude_subscription";

/**
 * An operator-owned substrate a target repo's `process.yaml` references by slug
 * (`api.schemas.RuntimeOut`). Slug and type are immutable after create — repos name the slug,
 * and credentials are shaped by the type — so a PATCH carries neither.
 *
 * `credentials` is every credential's name and the row's own clocks, nothing more: a value
 * leaves the store into a run and nowhere else, so there is no field a page could echo.
 * `credential_names` is the same list as names alone, for the callers that need only that.
 */
export interface Runtime {
  id: number;
  slug: string;
  name: string;
  type: RuntimeType;
  /** A per-type bag: `isolation`, on Bedrock its `region`/`model`, and on an E2B runtime its `template` build. */
  details: Record<string, unknown>;
  /** Operator notes, flat string to string. Edited as rows on the runtime page; no run ever
   *  reads it. */
  metadata: Record<string, string>;
  credential_names: string[];
  credentials: RuntimeCredential[];
  /** Projects whose latest process version names this slug — the same rule the projects
   *  list's Runtime column and `GET /api/runtimes/{id}/projects` read. */
  connected_projects: number;
  /** The newest run on this runtime that failed or idled on a refused credential. */
  last_auth_failure: AuthFailure | null;
  created_at: string;
  updated_at: string;
}

/** `api.schemas.AuthFailureOut`. `ticket` is `<owner/repo>#<issue>`. */
export interface AuthFailure {
  at: string;
  reason: string;
  ticket: string;
  ticket_id: number;
}

/** `api.schemas.CredentialCheckOut`: `detail` is a fixed sentence, never the vendor's body. */
export interface CredentialCheck {
  ok: boolean;
  detail: string;
}

/** `POST /api/runtimes/{id}/test-credential`. `local`: a codex subscription's stored login was
 *  read for its expiry, and OpenAI was not asked. `e2b`: an E2B runtime's team key. */
export interface CredentialTest extends CredentialCheck {
  local: boolean;
  e2b: CredentialCheck | null;
}

/** One project bound to a runtime (`GET /api/runtimes/{id}/projects`). */
/** `api.schemas.RuntimeTemplateBuildOut`: a ready build in an E2B runtime's team. */
export interface RuntimeTemplateBuild {
  pin: string;
  template: string;
  build_id: string;
  created_at: string;
  cpu_count: number;
  memory_mb: number;
  matches_harness: boolean;
  /** Whether it passed the session smoke: the worker refuses every other build. */
  verified: boolean;
}

export interface RuntimeProject {
  id: number;
  name: string;
  github_repo: string;
  /** Absent on a payload from before the list carried it; that reads as GitHub. */
  forge?: ForgeDisplay | null;
}

/** `api.schemas.RuntimeCredentialOut`. `updated_at` moves when the value is replaced;
 *  `created_at` is when it was first stored. */
export interface RuntimeCredential {
  name: string;
  created_at: string;
  updated_at: string;
}

export interface RuntimeIn {
  slug: string;
  name: string;
  type: RuntimeType;
  details?: Record<string, unknown>;
  metadata?: Record<string, string>;
  /** Credential name to value, stored with the row in the same request. Write-only. */
  credentials?: Record<string, string>;
}

/** `POST /api/orgs/{org}/runtimes/check`: the keys are tested before any runtime exists. */
export interface RuntimeCheckIn {
  type: RuntimeType;
  details: Record<string, unknown>;
  credentials: Record<string, string>;
}

export interface RuntimeCheckOut {
  model: CredentialCheck;
  local: boolean;
  e2b: CredentialCheck | null;
  templates: RuntimeTemplateBuild[];
}

/** `GET /api/runtime-options`: `<runtime.type>@<details.isolation>` entries, sorted. */
export interface RuntimeOptions {
  offered: string[];
}

/** A present `details` or `metadata` replaces the whole map. */
export interface RuntimePatch {
  name?: string;
  details?: Record<string, unknown>;
  metadata?: Record<string, string>;
}

/** A device-auth login the API started (`POST /runtimes/:id/login`, 202). The code is shown to
 *  the operator and goes nowhere else — the server writes it to no event and no log. */
export interface RuntimeLogin {
  session_id: string;
  verification_url: string;
  user_code: string;
  expires_at: string;
}

export type RuntimeLoginState = "pending" | "complete" | "failed" | "expired";

export interface RuntimeLoginStatus {
  session_id: string;
  status: RuntimeLoginState;
  /** The stderr tail on `failed`; null otherwise. */
  detail: string | null;
}

// --- request bodies ---------------------------------------------------------------------------

export interface ProjectIn {
  name: string;
  github_repo: string;
  description?: string | null;
  settings?: Record<string, unknown>;
}

/** Entries to add to and remove from one settings list, applied on the server to its current
 *  value — `PATCH /projects/{id}/permits`. */
export interface ListChange {
  add?: string[];
  remove?: string[];
}

export interface PermitsChange {
  runtime_allowlist?: ListChange;
  egress_allowlist?: ListChange;
}

export interface ProjectPatch {
  name?: string;
  description?: string | null;
  github_repo?: string;
  settings?: Record<string, unknown>;
}

export interface TicketSourceIn {
  kind?: "github" | "gitlab";
  external_ref: string;
  webhook_secret_ref?: string | null;
}

export interface ToolCall {
  tool: string;
  target: string | null;
  /** `null` while the call is still outstanding — a live run's last call has no result yet. */
  ok: boolean | null;
  result: string | null;
  /** `AskUserQuestion` only: the agent's questions and the human's answers, as plain text. */
  question: string | null;
  answer: string | null;
}

export interface ActivityTurn {
  index: number;
  /** Free-form model output, unlike tool fields. Truncated server-side. */
  text: string[];
  thinking: string[];
  tools: ToolCall[];
}

/** One published URL. `service` is null for a URL an outside producer published. */
export interface PreviewLink {
  service: string | null;
  url: string;
}

export type PreviewStatus = "none" | "starting" | "ready" | "failed" | "stopping" | "stopped";

/** A declared file's statuses (decision 71): `pending` until the worker has looked. */
export type OutputStatus = "pending" | "published" | "missing" | "ambiguous" | "lookup_failed";

/** One entry of a run's outputs: a file its step declares (`preview.files`), as the worker recorded
 *  it for the run. `kind` is open: a kind the dashboard does not know still shows by its label and
 *  links. */
export interface OutputEntry {
  kind: string;
  /** The declared path template; for a file a pattern matched, the template and the path joined
   *  by `#`. It names the entry across attempts. */
  key: string;
  label: string;
  status: OutputStatus;
  path: string | null;
  commit: string | null;
  size: number | null;
  /** Over the size the API serves: linked on GitHub, never framed. */
  too_large: boolean;
  matches: string[];
  reason: string | null;
  /** The signed link the frame loads, relative to the API; only for a file it will serve. */
  url: string | null;
  github_url: string | null;
  /** Whether this attempt changed the file; null where the record could not tell. */
  changed: boolean | null;
  /** The pattern's label, on every entry a pattern gives; null for a file named exactly. */
  group: string | null;
  /** How many files the pattern matched, shown or not. */
  group_total: number | null;
  /** The pattern's folder on GitHub at the attempt's commit, where every file it matched is. */
  group_url: string | null;
}

/** A path the step left on its branch, linked on GitHub; listed, never served. */
export interface ChangedFile {
  path: string;
  github_url: string;
}

/** `GET /step-runs/{id}/outputs`: each declared file, in declaration order. */
export interface RunOutputs {
  step_run_id: number;
  entries: OutputEntry[];
  /** The record reached GitHub's comparison limit, so a file may be missing from it. */
  cut_off: boolean;
  /** Every path the record holds that is still on the branch, sorted. */
  changed_files: ChangedFile[];
}

/** `GET /step-runs/{id}/preview`: one run's preview, never "the ticket's latest". */
export interface RunPreview {
  step_run_id: number;
  /** First-declared service first. Empty: nothing is published — not yet, or withdrawn. */
  urls: PreviewLink[];
  status: PreviewStatus;
  /** Why it failed or stopped. It may quote the sandbox's output: show it as text. */
  reason: string | null;
  /** The run's own sandbox serves this step's preview (`preview.services`). */
  served_by_runtime: boolean;
  startable: boolean;
  /** Why Start is not offered, for a runtime-served step. */
  unavailable: string | null;
  head_sha: string | null;
  uncommitted: boolean;
  /** When it closes for idle unless someone keeps it; null while its run is live or nothing serves. */
  stops_at: string | null;
  /** A live or open session shares its sandbox: the app's server reaches only its own ports. */
  beside_session: boolean;
  /** Its sandbox renewed at E2B's cap, and the loop is starting it again. */
  renewing: boolean;
  /** Serving now, so Restart is offered. */
  restartable: boolean;
}

/** `POST /step-runs/{id}/changes`: the request, and what the pass it woke made of it. */
export interface ChangeRequestResult extends RequestResult {
  request_id: number;
  /** `accepted`: the gate rejected and the rework is starting. `pending`: the pass has not
   *  reached it yet. `refused`: the loop could not honour it, and `reason` says why. */
  outcome: "pending" | "accepted" | "refused";
  reason: string | null;
}

/** `POST /step-runs/{id}/complete` and its poll: a Force complete and what the loop made of it
 *  (decision 78). `refused` carries `reason`, and every false leaf in `failures` when the
 *  postconditions do not hold. */
export interface CompletionResult extends RequestResult {
  request_id: number;
  outcome: "pending" | "accepted" | "forced" | "refused";
  reason: string | null;
  failures: string[];
  /** A person may continue past this refusal, having read `failures`. */
  overridable: boolean;
}

export interface RunActivityResponse {
  step_run_id: number;
  runtime_run_id: string;
  turns: ActivityTurn[];
  milestones: string[];
  outcome: string | null;
  cost_usd: number | null;
  truncated: boolean;
}

export interface LintWarning {
  code: string;
  loc: string;
  msg: string;
}

/** Verdict from the validate-only endpoint: errors are the parser's (unusable), warnings the
 *  lint layer's (parses green, would misbehave). */
export interface ProcessValidateResult {
  valid: boolean;
  errors: string[];
  warnings: LintWarning[];
  /** The server's parsed read of the text, when valid — what previews draw from. */
  content?: Record<string, unknown> | null;
}

/** `api.schemas.ProcessTemplateOut`: a whole process file the editor starts from. */
export interface ProcessTemplate {
  key: string;
  title: string;
  description: string;
  /** Every process file in the template's folder, in name order; an import writes them all. */
  lanes: TemplateLane[];
}

/** `api.schemas.TemplateLaneOut`: one lane of a template, one process file of its folder. */
export interface TemplateLane {
  key: string;
  /** Comments included. The editor sets its own `runtime:` line on it. */
  yaml: string;
  /** The parse, in a version snapshot's shape, for the gallery's graph. */
  content: Record<string, unknown>;
  /** What a first commit of it would write for each agent it names. */
  agents: AgentDetail[];
}

/** One file of the pinned `.factorial/` (`api.schemas.ConfigFileOut`). */
export interface ConfigFile {
  path: string;
  kind: "process" | "agent" | "prompt" | "skill" | "mcp" | "other";
  /** How this file stands against the repo's head. `unknown` when the head could not be read —
   *  never an optimistic `same`, because a silent "no drift" is the one wrong answer here. */
  drift: "same" | "differs" | "added" | "removed" | "unknown";
  /** Why this file cannot be used, if it cannot. Only agent manifests carry one, and only for
   *  agents the pinned process actually names. */
  warning: string | null;
  /** The git blob at the pinned commit, sent back as an edit's base. Null for a file only the
   *  head has. */
  sha: string | null;
}

export interface ConfigFiles {
  config_dir: string;
  pinned_sha: string;
  /** Null when the head could not be read, which is also when every `drift` is `unknown`. */
  head_sha: string | null;
  files: ConfigFile[];
}

export interface ConfigFileContent {
  path: string;
  content: string;
}

/** `GET /process-versions/{id}/agents`: one agent a version's process names, read at its pinned
 *  commit. When the manifest does not resolve, only `name` and `warning` are set. */
export interface AgentDetail {
  name: string;
  warning: string | null;
  model: string | null;
  max_turns: number | null;
  skills_local: string[];
  skills_shared: string[];
  mcp_local: string[];
  mcp_shared: string[];
  secrets: string[];
  /** Null when the manifest asks for nothing, which means the default profile — not no access. */
  permissions: Record<string, string> | null;
}

export interface ProcessVersionRaw {
  content: string;
  /** "repo" = the file at the pinned commit, comments intact; "snapshot" = re-emitted parse. */
  source: "repo" | "snapshot";
}

export interface LabelSpec {
  name: string;
  color: string;
  description: string;
  /** null when the project has no GitHub connection: expected, not verified. */
  exists: boolean | null;
}

/** Who a mint from this browser would record (`api.auth.Minter`). The session wins over a bearer. */
export interface McpMinter {
  email: string | null;
  via: "session" | "api_token";
  /** A session signed in more than 24 hours ago: the API refuses a mint with 403 until a fresh
   *  sign-in, never 401, which would sign the reader out. */
  reauth_required: boolean;
}

/** One MCP token (`api.schemas.McpTokenOut`). Never the token or its jti. */
export interface McpTokenRow {
  id: number;
  label: string;
  access: "read" | "write";
  created_by: string;
  minted_via: "session" | "api_token";
  created_at: string;
  expires_at: string;
  revoked_at: string | null;
  revoked_by: string | null;
  last_used_at: string | null;
  last_used_client: string | null;
  last_used_ip: string | null;
  /** `rotated`: signed under an API token this deployment no longer holds, so it no longer works. */
  state: "active" | "expired" | "revoked" | "rotated";
}

export interface McpTokenIndex {
  minter: McpMinter;
  /** The address a client is pointed at. */
  mcp_url: string;
  tokens: McpTokenRow[];
}

export interface McpTokenMintIn {
  label: string;
  access: "read" | "write";
}

/** The one answer that carries the token itself. */
export interface McpTokenMinted {
  token: string;
  row: McpTokenRow;
}

export interface McpTool {
  name: string;
  title: string | null;
  description: string;
  scope: "factorial:read" | "factorial:write";
  destructive: boolean;
}

export interface Version {
  revision: string | null;
}

export interface McpToolCatalog {
  tools: McpTool[];
}

// --- The assistant's settings (`api.schemas.AssistantSettingsOut`) ----------------------------------

/** Decimals travel as strings, to keep their scale. */
export interface AssistantSettings {
  enabled: boolean;
  backend: string;
  auth_mode: string;
  model: string;
  effort: "low" | "medium" | "high";
  turn_budget_usd: string;
  max_steps: number;
  daily_budget_usd: string;
  /** Everyone's spend together in one day; null sets no cap. */
  organization_daily_budget_usd: string | null;
  bedrock_region: string;
}

export interface AssistantBackend {
  kind: string;
  label: string;
  auth_modes: string[];
  /** auth mode -> the model a switch to it starts from. */
  default_models: Record<string, string>;
}

/** The stored credential's provenance, never the credential. */
export interface AssistantCredential {
  set: boolean;
  updated_by: string | null;
  updated_at: string | null;
}

/** What the assistant's process last found when it checked its backend. */
export interface AssistantStatus {
  ok: boolean;
  message: string | null;
  version?: string | null;
  at?: string | null;
}

/** One stored credential's place and provenance (`api.schemas.AssistantStoredCredentialOut`). */
export interface AssistantStoredCredential {
  backend: string;
  auth_mode: string;
  updated_by: string;
  updated_at: string;
}

export interface AssistantSettingsOut {
  settings: AssistantSettings;
  /** Every stored credential, the saved backend and auth mode's among them. */
  credentials: AssistantStoredCredential[];
  /** The sign-ins a person may switch to, as `<backend>:<auth_mode>`; `backends` adds the saved
   *  one when the list dropped it. Absent from an API older than the list. */
  offered?: string[];
  backends: AssistantBackend[];
  /** Same gate as POST …/messages: enabled plus credential or Bedrock mint. */
  ready_to_send: boolean;
  status: AssistantStatus | null;
}

/** `GET …/settings/assistant/ready`: what the chat shows a reader who cannot read the settings. */
export interface AssistantReadyOut {
  enabled: boolean;
  credential_set: boolean;
  /** Changes with the credential, so a chat holding a refusal sees the fix. */
  credential_updated_at: string | null;
  ready_to_send: boolean;
}

export interface ChatItem {
  id: number;
  turn_id: number | null;
  kind: string;
  payload: Record<string, unknown>;
  final: boolean;
  rev: number;
}

export type ChatTurnStatus = "queued" | "running" | "done" | "failed" | "cancelled" | "interrupted";

export interface ChatTurn {
  id: number;
  status: ChatTurnStatus;
  stop_reason: string | null;
  failure_reason: string | null;
  failure_message: string | null;
  cost_usd: string | null;
  cost_source: string | null;
}

export interface ChatItems {
  items: ChatItem[];
  turn: ChatTurn | null;
}

// --- The assistant's conversations (`api.schemas.ChatConversationOut`) and the cards its items
// carry. A card's payload is written by `mcp_server/chat/tools.py` or `assistant/proposals.py`;
// what a model put in one is read as text, never as markup.

export interface ChatConversation {
  id: number;
  title: string | null;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
}

/** `factorial_ui_show`'s card: a dashboard route, and the control on it to point at. */
export interface ShowMePayload {
  path: string;
  anchor: string | null;
  note: string | null;
}

export interface ChartSeries {
  name: string;
  values: number[];
}

/** `factorial_chart`'s card: one value per `x` label in each series. */
export interface ChartPayload {
  kind: "line" | "bar" | "grouped_bar";
  title: string;
  unit: "USD" | "minutes" | "hours" | "tickets" | "tokens";
  x: string[];
  series: ChartSeries[];
  source: string;
}

/** `factorial_editor_open`'s card, which `GET /api/assistant/drafts/{item id}` returns. A file
 *  with no body takes the catalog's. */
export interface EditorDraftPayload {
  project_id: number;
  process: string;
  content: string;
  files: { path: string; body: string | null }[];
  edits: ConfigEdit[];
  base_sha: string | null;
  would_write: { path: string; origin: string }[];
}

export type ProposalStatus =
  | "pending"
  | "approved"
  | "succeeded"
  | "refused"
  | "declined"
  | "expired";

/** A write the model asked for, held for its conversation's owner (`assistant/proposals.py`). */
export interface ProposalPayload {
  proposal_id: number;
  tool: string;
  title: string;
  preview: Record<string, unknown>;
  status: ProposalStatus;
  expires_at: string;
  decided_by: string | null;
  decided_at: string | null;
  result: Record<string, unknown> | null;
}

/** What Approve and Decline answer (`mcp_server/chat/twins.py::ProposalOut`). */
export interface ProposalDecision {
  proposal_id: number;
  status: ProposalStatus;
  card_item_id: number;
  result: Record<string, unknown> | null;
}
