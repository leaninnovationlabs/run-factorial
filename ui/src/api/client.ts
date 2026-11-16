/**
 * The one place that talks to the orchestrator.
 *
 * Three things it is careful about:
 *
 * - **A 401 is a distinct condition, not just another error.** It means the session has lapsed,
 *   so the app has to sign the reader in again.
 *   `ApiError.unauthorized` is what the auth layer keys on.
 * - **`detail` is where the useful message lives.** FastAPI puts the operator-facing reason
 *   there — which blocker prevented activation, which line of `process.yaml` failed to parse —
 *   and dropping it in favour of the status code makes every failure look the same.
 * - **Except when the API never answered, or failed without saying why.** Status 0, a 500 (the
 *   API's catch-all answers an unhandled exception with "internal server error") and any other
 *   5xx whose body carries no `detail` (a proxy's own page) read as one plain sentence
 *   (`UNREACHABLE`, `SERVER_FAILED`), and what the browser or the server said goes on
 *   `ApiError.detail`, which the error boxes keep behind Details for an operator. A 503 or 502 the
 *   API wrote a `detail` for is a refusal like any other: "this project has no GitHub connection" is
 *   the next step, not noise.
 */

import { APP_PREFIX } from "../prefix";
import type {
  Account,
  AgentDetail,
  AssistantReadyOut,
  AssistantSettings,
  AssistantSettingsOut,
  BoardResponse,
  ChangeRequestResult,
  ChatConversation,
  ChatItems,
  CompletionResult,
  ConfigAdoption,
  ConfigBranchResult,
  ConfigCommitResult,
  ConfigDelete,
  ConfigEdit,
  ConfigFileContent,
  ConfigFiles,
  ConfigPreview,
  CredentialTest,
  DefaultBranchConfig,
  DraftCheck,
  EditorDraftPayload,
  FleetRefreshResult,
  GatePullRequest,
  GitHubConnection,
  GitHubConnectionIn,
  Invite,
  InviteCreated,
  InvitePreview,
  LabelSpec,
  McpTokenIndex,
  McpTokenMinted,
  McpTokenMintIn,
  McpToolCatalog,
  Member,
  Organization,
  OrganizationDetail,
  OverviewResponse,
  PendingIssue,
  Permission,
  PermitsChange,
  ProcessSync,
  ProcessTemplate,
  ProcessValidateResult,
  ProcessVersion,
  ProcessVersionRaw,
  Project,
  ProjectDashboard,
  ProjectDetail,
  ProjectIn,
  ProjectListItem,
  ProjectPatch,
  ProjectReadiness,
  ProposalDecision,
  RefreshResult,
  RequestResult,
  RestartPreview,
  RestorePreview,
  Review,
  Role,
  RunActivityResponse,
  RunOutputs,
  RunPreview,
  Runtime,
  RuntimeCheckIn,
  RuntimeCheckOut,
  RuntimeIn,
  RuntimeLogin,
  RuntimeLoginStatus,
  RuntimeOptions,
  RuntimePatch,
  RuntimeProject,
  RuntimeTemplateBuild,
  SecretDelivery,
  SecretDeliveryWrite,
  SecretWrite,
  SeedSource,
  SelectedFile,
  Span,
  TerminalInfo,
  TicketDetail,
  TicketIssue,
  TicketListResponse,
  TicketProcess,
  TicketSource,
  TicketSourceIn,
  TicketStatus,
  TimelineResponse,
  UnknownHead,
  Version,
} from "./types";

/** The API's origin (empty: this one), trailing slash trimmed so `${base}/api/...` never
 *  produces a double slash. */
function resolveApiOrigin(): string {
  const configured = (import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000").replace(
    /\/+$/,
    "",
  );
  if (typeof window === "undefined") return configured;
  try {
    const api = new URL(configured);
    const pageHost = window.location.hostname;
    const loopback = new Set(["localhost", "127.0.0.1"]);
    // ALB/HTTPS: /api and /auth are on this origin. Keep :8000 only for local loopback.
    if (loopback.has(api.hostname) && !loopback.has(pageHost)) {
      return window.location.origin;
    }
  } catch {
    return configured;
  }
  return configured;
}

/** The API's origin and the app's prefix. */
const BASE_URL = `${resolveApiOrigin()}${APP_PREFIX}`;

/** An API path as an absolute URL, for what the browser loads itself: a framed page has no
 *  `request` to carry the base. */
export function apiUrl(path: string): string {
  return `${BASE_URL}${path}`;
}

/** What a reader is told when the API did not answer at all. */
export const UNREACHABLE = "Couldn't reach Factorial. Check your connection and try again.";
/** What a reader is told for a 5xx: the server's own words are for an operator, under Details. */
export const SERVER_FAILED = "Factorial couldn't complete the request. Try again.";

export class ApiError extends Error {
  readonly status: number;
  /** The closed word a refusal names beside its `detail` where the API gives one: the assistant's
   *  `turn_live`, `not_configured`, `daily_budget`. */
  readonly code: string | null;
  /** The next step a refusal names beside its `detail`, where the API gives one. */
  readonly hint: string | null;
  /** The technical text behind a plain message: why the browser could not reach the API, or what
   *  the server said with its 5xx. Null where the message is the server's own. */
  readonly detail: string | null;
  /** The runtime slugs a sync named that do not exist, beside `code: "runtime_missing"`. */
  readonly missingRuntimes: string[] | null;
  /** The config file a refusal is about, where the API names one. */
  readonly path: string | null;

  constructor(
    status: number,
    message: string,
    code: string | null = null,
    {
      hint = null,
      detail = null,
      missingRuntimes = null,
      path = null,
    }: {
      hint?: string | null;
      detail?: string | null;
      missingRuntimes?: string[] | null;
      path?: string | null;
    } = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.hint = hint;
    this.detail = detail;
    this.missingRuntimes = missingRuntimes;
    this.path = path;
  }

  get unauthorized(): boolean {
    return this.status === 401;
  }
}

export function isNotFound(error: unknown): boolean {
  return typeof error === "object" && error !== null && "status" in error && error.status === 404;
}

/**
 * The organization this page acts in (tech-stack decision 68): the `/orgs/<slug>` the URL
 * opened under, set once before the first render (`org.ts`). Fixed for the page's life: moving
 * to another organization is a full load, so no cached answer can be another organization's.
 */
let organization = "default";

export function setOrganization(slug: string): void {
  organization = slug;
}

/** An org-scoped API path: `/api/orgs/<slug>` and `rest`. */
function orgPath(rest: string): string {
  return `/api/orgs/${encodeURIComponent(organization)}${rest}`;
}

function requestUrl(path: string): string {
  // Same-origin so Vite (`pnpm dev`) can proxy the prefix's /auth; the API serves the image's
  // dashboard, so there it is the API already.
  return path.startsWith("/auth") ? `${APP_PREFIX}${path}` : `${BASE_URL}${path}`;
}

async function call(path: string, init: RequestInit): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(requestUrl(path), {
      ...init,
      credentials: init.credentials ?? "include",
      headers: {
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...init.headers,
      },
    });
  } catch (cause) {
    // A stream the reader closed on purpose is not a fault to explain.
    if (init.signal?.aborted) throw cause;
    // fetch only rejects for network-level failures, and in a browser they are indistinguishable
    // from CORS rejections — which is by far the likelier cause here, so the details say so.
    throw new ApiError(0, UNREACHABLE, null, {
      detail: `Can't reach the API at ${BASE_URL}. Check that it is running and that FACTORIAL_CORS_ORIGINS includes this origin. (${String(cause)})`,
    });
  }
  if (!response.ok) throw await refusal(response);
  return response;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await call(path, init);
  // A 204 is not the only empty answer: Stop's 202 has no body either.
  const body = await response.text();
  return (body === "" ? undefined : JSON.parse(body)) as T;
}

async function refusal(response: Response): Promise<ApiError> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    // Not JSON — the status line is all there is.
  }
  const fields = body as
    | { code?: unknown; hint?: unknown; missing_runtimes?: unknown; path?: unknown }
    | undefined;
  const slugs = fields?.missing_runtimes;
  const missingRuntimes =
    Array.isArray(slugs) && slugs.every((slug) => typeof slug === "string")
      ? (slugs as string[])
      : null;
  const code = typeof fields?.code === "string" ? fields.code : null;
  const hint = typeof fields?.hint === "string" && fields.hint !== "" ? fields.hint : null;
  const said = errorDetail(response, body);
  if (response.status === 500 || (response.status > 500 && !explained(body))) {
    return new ApiError(response.status, SERVER_FAILED, code, { hint, detail: said });
  }
  const path = typeof fields?.path === "string" && fields.path !== "" ? fields.path : null;
  return new ApiError(response.status, said, code, { hint, missingRuntimes, path });
}

function explained(body: unknown): boolean {
  const detail = (body as { detail?: unknown } | undefined)?.detail;
  return typeof detail === "string" && detail.trim() !== "";
}

function errorDetail(response: Response, body: unknown): string {
  if (typeof body === "object" && body !== null) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
    // 422s from FastAPI's own validation are a list of per-field objects.
    if (Array.isArray(detail)) {
      return detail
        .map((item) => {
          const { loc, msg } = item as { loc?: unknown[]; msg?: string };
          const where = Array.isArray(loc) ? loc.slice(1).join(".") : "";
          const said = where ? `${where}: ${msg}` : (msg ?? JSON.stringify(item));
          return /[.!?]$/.test(said) ? said : `${said}.`;
        })
        .join(" ");
    }
    if (detail !== undefined) return JSON.stringify(detail);
  }
  return `${response.status} ${response.statusText}`;
}

/** A write. The body, when there is one, goes as JSON. */
function send<T>(
  method: "POST" | "PUT" | "PATCH" | "DELETE",
  path: string,
  body?: unknown,
): Promise<T> {
  return request<T>(path, body === undefined ? { method } : { method, body: JSON.stringify(body) });
}

function query(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== false) search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

/** Where the Google sign-in starts — a navigation, not a fetch: the browser must follow
 * the redirect chain to Google and back. `next` is the path the callback returns to; the API
 * accepts only a path on its own origin. */
export type AuthProviders = {
  google: boolean;
  oidc: boolean;
  oidc_label: string;
  /** Every deployment signs people in with a password (design.md §7.19). */
  password?: boolean;
  /** A stranger may create an account here: multi-org is on, or the install is empty and its
   *  first account becomes the owner. An invite's link offers signup whatever this says. */
  signup?: boolean;
  /** Many organizations: anyone signs up, and any account creates organizations. */
  multi_org?: boolean;
  /** The install is empty and its first account must send FACTORIAL_AUTH__BOOTSTRAP_TOKEN. */
  setup_token?: boolean;
  /** The deployment sends email: a password signup verifies its address, and a forgotten
   *  password can be reset (design.md §7.19 items 16 and 17). */
  mail?: boolean;
};

export type AuthMe = {
  email: string;
  verified?: boolean;
  /** Mail is configured and this session's email is not verified: it reaches nothing until the
   *  link in its email is opened. */
  must_verify?: boolean;
};

export function googleLoginUrl(next = "/"): string {
  const back = next !== "/" && next.startsWith("/") ? `?next=${encodeURIComponent(next)}` : "";
  return `${APP_PREFIX}/auth/login${back}`;
}

export function oidcLoginUrl(next = "/"): string {
  const back = next !== "/" && next.startsWith("/") ? `?next=${encodeURIComponent(next)}` : "";
  // Same-origin like a same-host deploy; `pnpm dev` proxies the prefix's /auth.
  return `${APP_PREFIX}/auth/oidc/login${back}`;
}

export const api = {
  listOrganizations: () => request<Organization[]>("/api/orgs"),

  authProviders: () => request<AuthProviders>("/auth/providers"),

  /** With the cookie even across origins: in development the API is another port, and the
   *  terminal connects by the Google session when there is one (decision 57). */
  authMe: () => request<AuthMe>("/auth/me", { credentials: "include" }),

  /** Send the signed-in account a new verification link. */
  sendVerification: () => send<void>("POST", "/auth/email/verification"),

  /** Redeem a verification link: only a session of the account it was sent to can. */
  verifyEmail: (token: string) => send<void>("POST", "/auth/email/verify", { token }),

  /** Answers the same whether the email has an account or not. */
  forgotPassword: (email: string) => send<void>("POST", "/auth/password/forgot", { email }),

  resetPassword: (body: { token: string; password: string }) =>
    send<void>("POST", "/auth/password/reset", body),

  authLogout: () => request<void>("/auth/logout", { method: "POST", credentials: "include" }),

  signUp: (body: {
    email: string;
    name: string;
    password: string;
    invite?: string;
    setup_token?: string;
  }) => send<Account>("POST", "/auth/signup", body),

  passwordSignIn: (body: { email: string; password: string }) =>
    send<Account>("POST", "/auth/password/login", body),

  /** The organization the page is in, with what the reader may do there. */
  getOrganization: () => request<OrganizationDetail>(orgPath("")),

  createOrganization: (body: { name: string; slug: string }) =>
    send<Organization>("POST", "/api/orgs", body),

  listRoles: () => request<Role[]>("/api/roles"),
  listPermissions: () => request<Permission[]>("/api/permissions"),

  listMembers: () => request<Member[]>(orgPath("/members")),

  changeMemberRole: (userId: number, role: string) =>
    send<void>("PATCH", orgPath(`/members/${userId}`), { role }),

  removeMember: (userId: number) => send<void>("DELETE", orgPath(`/members/${userId}`)),

  listInvites: () => request<Invite[]>(orgPath("/invites")),

  createInvite: (body: { email: string; role: string }) =>
    send<InviteCreated>("POST", orgPath("/invites"), body),

  revokeInvite: (id: number) => send<void>("DELETE", orgPath(`/invites/${id}`)),

  previewInvite: (token: string) =>
    request<InvitePreview>(`/api/invites/${encodeURIComponent(token)}`),

  acceptInvite: (token: string) =>
    send<{ organization_slug: string; role: string }>(
      "POST",
      `/api/invites/${encodeURIComponent(token)}/accept`,
    ),

  listProjects: () => request<ProjectListItem[]>(orgPath("/projects")),

  getProject: (id: number) => request<ProjectDetail>(`/api/projects/${id}`),

  /** The repo-side readiness rows, off the project page's critical path. */
  getProjectReadiness: (id: number) => request<ProjectReadiness>(`/api/projects/${id}/readiness`),

  createProject: (body: ProjectIn) => send<Project>("POST", orgPath("/projects"), body),

  updateProject: (id: number, body: ProjectPatch) =>
    send<Project>("PATCH", `/api/projects/${id}`, body),

  changePermits: (id: number, body: PermitsChange) =>
    send<Project>("PATCH", `/api/projects/${id}/permits`, body),

  setProjectActive: (id: number, active: boolean) =>
    send<Project>("PUT", `/api/projects/${id}/active`, { active }),

  putTicketSource: (id: number, body: TicketSourceIn) =>
    send<TicketSource>("PUT", `/api/projects/${id}/ticket-source`, {
      kind: body.kind ?? "github",
      ...body,
    }),

  getGitHubConnection: (id: number) =>
    request<GitHubConnection>(`/api/projects/${id}/github-connection`),

  putGitHubConnection: (id: number, body: GitHubConnectionIn) =>
    send<GitHubConnection>("PUT", `/api/projects/${id}/github-connection`, body),

  putSecret: (id: number, name: string, body: SecretWrite) =>
    send<{ name: string }>("PUT", `/api/projects/${id}/secrets/${encodeURIComponent(name)}`, body),

  patchSecretDelivery: (id: number, name: string, body: SecretDeliveryWrite) =>
    send<SecretDelivery>("PATCH", `/api/projects/${id}/secrets/${encodeURIComponent(name)}`, body),

  deleteSecret: (id: number, name: string) =>
    send<void>("DELETE", `/api/projects/${id}/secrets/${encodeURIComponent(name)}`),

  createLabels: (id: number) => send<{ created: string[] }>("POST", `/api/projects/${id}/labels`),

  /** Create the `factorial` branch: a copy of the default branch's config folder, or with
   *  `"empty"` only a README, for an operator starting from a template. */
  createConfigBranch: (id: number, from: SeedSource = "default_branch") =>
    send<ConfigBranchResult>("POST", `/api/projects/${id}/config-branch`, { from }),

  /** The commits and files a direct push added to `factorial` since the last known commit. */
  getUnknownHead: (id: number) =>
    request<UnknownHead>(`/api/projects/${id}/config-branch/unknown-head`),

  /** Vouch for the head the person reviewed, by its full sha, then sync it. */
  adoptConfigHead: (id: number, sha: string) =>
    send<ConfigAdoption>("POST", `/api/projects/${id}/config-branch/adoptions`, { sha }),

  /** The default branch's config folder as a seed would copy it, or null when it has none. */
  getDefaultBranchConfig: (id: number) =>
    request<DefaultBranchConfig | null>(`/api/projects/${id}/default-branch-config`),

  /** Snapshot every lane the repo offers, at one sha (design.md §2.8). `ref` null = the repo's
   *  `factorial` branch. One outcome per lane comes back — see `ProcessSyncResult`. */
  syncProcessVersions: (id: number, ref: string | null) =>
    send<ProcessSync>("POST", `/api/projects/${id}/process-versions/sync`, { ref }),

  /** Choose the lane an issue with no `factorial:process:` label enters. `null` clears it, which
   *  is allowed and then requires every issue to carry a label. A settings write, never a repo
   *  edit: the default is operator state Postgres owns (design.md §7.5). */
  setDefaultProcess: (id: number, processKey: string | null) =>
    send<Project>("PUT", `/api/projects/${id}/default-process`, { process_key: processKey }),

  /** Every open issue the admission pass declines, and why — recomputed, never read back from
   *  the event log. */
  listPendingIssues: (id: number) => request<PendingIssue[]>(`/api/projects/${id}/pending`),

  /** What a commit from this draft would create under `.factorial/`. Reads the repo; writes
   *  nothing. The file explorer's tree is built from the result. */
  previewConfigBundle: (id: number, content: string) =>
    send<ConfigPreview>("POST", `/api/projects/${id}/config-preview`, { content }),

  /** The App commits the process file, plus every file the operator assembled, straight onto
   *  `factorial`, then syncs exactly that commit (design.md §7.7). 409 when the branch is
   *  missing, its head is unknown, it moved while committing, or GitHub refused the commit; 503
   *  when the project has no GitHub connection — the copy/download path stays the fallback. A sync that fails after the commit landed still answers 201, with
   *  `sync_error` set.
   *
   *  `content` is committed byte for byte, so hand edits survive. `files` may name only paths
   *  the server's own freshly-recomputed proposal for `content` still contains — it refuses
   *  anything else, the process file included — that always comes from `content`. `edits` rewrite
   *  files the branch already has, each from the blob it started from; 409 when that blob moved,
   *  422 for a path that is not a file under the config folder on the branch, one sent twice, or
   *  the lane's own process file. `created` are new files: 422 for a path outside the config
   *  folder, with an unsafe name, or with anything on the branch at, above or inside it.
   *  `deleted` are files the branch has, each at the blob it was listed at: 409 when that blob
   *  moved, 422 for a path outside the config folder, one also written by this commit, or the
   *  last process. One the branch no longer has is skipped. */
  commitConfig: (
    id: number,
    content: string,
    files: SelectedFile[],
    edits: ConfigEdit[],
    created: SelectedFile[] = [],
    deleted: ConfigDelete[] = [],
  ) =>
    send<ConfigCommitResult>("POST", `/api/projects/${id}/config-commits`, {
      content,
      files,
      edits,
      created,
      deleted,
    }),

  /** Every snapshot of every process, the current one first within each process. */
  listProcessVersions: (id: number) =>
    request<ProcessVersion[]>(`/api/projects/${id}/process-versions`),

  /** What restoring the config folder to this version would write, delete and retire. Writes
   *  nothing. 409 while the branch head is one no one vouches for. */
  getRestorePreview: (id: number, versionId: number) =>
    request<RestorePreview>(`/api/projects/${id}/process-versions/${versionId}/restore`),

  /** Commits the config folder back to a synced version's, as a new commit on `factorial`, and
   *  syncs it. 409 when the head is no longer `expectedHead`, the one the preview read. */
  restoreConfig: (id: number, versionId: number, expectedHead: string) =>
    send<ConfigCommitResult>("POST", `/api/projects/${id}/config-restores`, {
      version_id: versionId,
      expected_head: expectedHead,
    }),

  /** `commitConfig`'s own checks on the same draft, as a verdict. Writes nothing. A draft that
   *  could be judged answers 200 either way; a forge that cannot be read is still an error. */
  checkConfig: (
    id: number,
    content: string,
    files: SelectedFile[],
    edits: ConfigEdit[],
    created: SelectedFile[] = [],
    deleted: ConfigDelete[] = [],
  ) =>
    send<DraftCheck>("POST", `/api/projects/${id}/config-checks`, {
      content,
      files,
      edits,
      created,
      deleted,
    }),

  /** One version WITH its stored parse — what a snapshot view draws its graph from.
   *
   *  These four take the version ROW's id, not its `version` number: two lanes may both be at
   *  v1, so the number alone stopped identifying a row. */
  getProcessVersion: (id: number, versionId: number) =>
    request<ProcessVersion & { content: Record<string, unknown> }>(
      `/api/projects/${id}/process-versions/${versionId}`,
    ),

  getProcessVersionRaw: (id: number, versionId: number) =>
    request<ProcessVersionRaw>(`/api/projects/${id}/process-versions/${versionId}/raw`),

  /** The whole `.factorial/` folder at a version's pinned commit — the explorer's listing. */
  listConfigFiles: (id: number, versionId: number) =>
    request<ConfigFiles>(`/api/projects/${id}/process-versions/${versionId}/files`),

  /** One config file's text at that pinned commit. The server proves the path is a member of
   *  its own listing before reading, so a path from the client cannot steer the read. */
  readConfigFile: (id: number, versionId: number, path: string) =>
    request<ConfigFileContent>(`/api/projects/${id}/process-versions/${versionId}/files/${path}`),

  /** Every agent a version's process names, parsed at its pinned commit. */
  listProcessVersionAgents: (id: number, versionId: number) =>
    request<AgentDetail[]>(`/api/projects/${id}/process-versions/${versionId}/agents`),

  /** Every label the pinned process uses, with its role — and whether the repo defines it. */
  listProjectLabels: (id: number) => request<{ labels: LabelSpec[] }>(`/api/projects/${id}/labels`),

  /** `shells` is what a new agent, skill, MCP server or process starts from: kind to file to
   *  body, with `__NAME__` where the typed name goes. */
  listProcessTemplates: () =>
    request<{ templates: ProcessTemplate[]; shells?: Record<string, Record<string, string>> }>(
      "/api/process-templates",
    ),

  /** Validate raw process.yaml text without storing anything. Always 200 with a verdict. */
  validateProcess: (id: number, content: string) =>
    send<ProcessValidateResult>("POST", `/api/projects/${id}/process-versions/validate`, {
      content,
    }),

  listTickets: (id: number, options: { status?: TicketStatus; openOnly?: boolean } = {}) =>
    request<TicketListResponse>(
      `/api/projects/${id}/tickets${query({ status: options.status, open_only: options.openOnly })}`,
    ),

  getBoard: (id: number) => request<BoardResponse>(`/api/projects/${id}/board`),

  /** The project dashboard's one read; `span` goes over the wire as itself, `all` included. */
  getDashboard: (id: number, span: Span) =>
    request<ProjectDashboard>(`/api/projects/${id}/dashboard${query({ span })}`),

  getTicket: (ticketId: number) => request<TicketDetail>(`/api/tickets/${ticketId}`),

  getTimeline: (ticketId: number) => request<TimelineResponse>(`/api/tickets/${ticketId}/timeline`),

  getTicketProcess: (ticketId: number) =>
    request<TicketProcess>(`/api/tickets/${ticketId}/process`),

  /** The ticket's issue as GitHub has it now. The one ticket read that leaves the database, so
   *  it is also the one that can be slow or answer 503 when the project has no GitHub connection. */
  getTicketIssue: (ticketId: number) => request<TicketIssue>(`/api/tickets/${ticketId}/issue`),
  getTicketGatePullRequest: (ticketId: number) =>
    request<GatePullRequest>(`/api/tickets/${ticketId}/gate-pull-request`),

  interruptRun: (stepRunId: number) =>
    send<RequestResult>("POST", `/api/step-runs/${stepRunId}/interrupt`),

  /** Start a failed step's next attempt now, continuing the failed session where it can. */
  retryStep: (stepRunId: number) =>
    send<RequestResult>("POST", `/api/step-runs/${stepRunId}/retry-step`),

  /** A fleet pass now — admission, then every ticket (decision 4). Answers 202 at once: nothing
   *  marks when a fleet pass is done, so the page's polling shows it. */
  refreshFleet: () => send<FleetRefreshResult>("POST", orgPath("/refresh")),

  /** One pass over the ticket now, which the API waits for (decision 4). */
  refreshTicket: (ticketId: number) =>
    send<RefreshResult>("POST", `/api/tickets/${ticketId}/refresh`),

  /** Exit, re-enter and unblock are REQUESTS the loop honours on the pass they wake (decision
   *  29): 202, not a transition. Each answers once that pass has landed, or when the wait runs
   *  out. */
  /** `close_issue` closes the issue as well: the gate message's Close and exit (decision 79). */
  exitTicket: (ticketId: number, body?: { close_issue: boolean }) =>
    send<RequestResult>("POST", `/api/tickets/${ticketId}/exit`, body),

  reenterTicket: (ticketId: number) =>
    send<RequestResult>("POST", `/api/tickets/${ticketId}/reenter`),

  unblockTicket: (ticketId: number) =>
    send<RequestResult>("POST", `/api/tickets/${ticketId}/unblock`),

  /** Unlike exit, reenter and unblock, this is not a request the loop reads later: `acquire_leases`
   *  excludes a quarantined ticket, so it would never lease one to observe the request. The
   *  route writes the ticket directly, and 202 still — the ticket query is what confirms it. */
  requeueTicket: (ticketId: number) =>
    send<RequestResult>("POST", `/api/tickets/${ticketId}/requeue`),

  /** What a restart would cancel, close and delete, read live off GitHub. A forecast: the worker
   *  works it out again from its own observation when it answers. */
  getRestartPreview: (ticketId: number) =>
    request<RestartPreview>(`/api/tickets/${ticketId}/restart/preview`),

  /** A REQUEST, like exit (#321): 202 once the pass it woke has landed. `process_version_id`
   *  names the snapshot to restart on, and the API refuses it once the lane has moved past it. */
  restartTicket: (
    ticketId: number,
    body: { process_version_id?: number; allow_current_pin?: boolean; allow_merged?: boolean },
  ) => send<RequestResult>("POST", `/api/tickets/${ticketId}/restart`, body),

  /** Redo a step and every step after it from a clean start (decision 76). Dashboard only. */
  redoStep: (ticketId: number, body: { target_step: string; note?: string }) =>
    send<RequestResult>("POST", `/api/tickets/${ticketId}/redo`, body),

  getRunActivity: (stepRunId: number) =>
    request<RunActivityResponse>(`/api/step-runs/${stepRunId}/activity`),

  getRunPreview: (stepRunId: number) => request<RunPreview>(`/api/step-runs/${stepRunId}/preview`),
  getRunOutputs: (stepRunId: number) => request<RunOutputs>(`/api/step-runs/${stepRunId}/outputs`),

  /** REQUESTS, like interrupt: the loop wakes or parks the run's kept sandbox on the pass this
   *  wakes, and the answer is the preview as that pass left it (decision 54). */
  startPreview: (stepRunId: number, restart = false) =>
    send<RunPreview>("POST", `/api/step-runs/${stepRunId}/preview/start`, { restart }),
  keepPreview: (stepRunId: number) =>
    send<RunPreview>("POST", `/api/step-runs/${stepRunId}/preview/keep`),
  stopPreview: (stepRunId: number) =>
    send<RunPreview>("POST", `/api/step-runs/${stepRunId}/preview/stop`),

  /** What the run's terminal offers now, by the gateway's own admission rule (decision 57). */
  getRunTerminal: (stepRunId: number) =>
    request<TerminalInfo>(`/api/step-runs/${stepRunId}/terminal`),

  /** A REQUEST to reopen the run's own agent session, read-only, in its kept workspace: the
   *  inspect pass starts it, and `getRunTerminal` shows it starting, then open or refused. */
  openSession: (stepRunId: number) =>
    send<TerminalInfo>("POST", `/api/step-runs/${stepRunId}/inspect`),

  /** A REQUEST to close the run's open session now, freeing its awake slot: the inspect pass ends
   *  it, and `getRunTerminal` then shows it closed. */
  closeSession: (stepRunId: number) =>
    send<TerminalInfo>("POST", `/api/step-runs/${stepRunId}/inspect/close`),

  /** A REQUEST to continue a parked session, `answer` as its next turn (decision 26).
   *  `pending` while an earlier one is unanswered. */
  resumeSession: (stepRunId: number, answer: string | null) =>
    send<RequestResult>("POST", `/api/step-runs/${stepRunId}/resume-session`, { answer }),

  /** A REQUEST, like interrupt: the loop rejects the review gate on the pass this wakes. The
   *  key is chosen once per form, so a retried submission answers with the first request. */
  requestChanges: (stepRunId: number, feedback: string, idempotencyKey: string) =>
    send<ChangeRequestResult>("POST", `/api/step-runs/${stepRunId}/changes`, {
      feedback,
      idempotency_key: idempotencyKey,
    }),

  /** A REQUEST to check the run's postconditions now and accept it when they hold (decision 78).
   *  `pending` while an earlier one for this run is unanswered; `getCompletion` polls the answer. */
  requestCompletion: (
    stepRunId: number,
    body: { allow_unmet?: boolean; seen_failures?: string[] },
  ) => send<CompletionResult>("POST", `/api/step-runs/${stepRunId}/complete`, body),

  getCompletion: (stepRunId: number, requestId: number) =>
    request<CompletionResult>(`/api/step-runs/${stepRunId}/completion/${requestId}`),

  /** What Approve on the run would approve. Reads the forge on every call. */
  getReview: (stepRunId: number) => request<Review>(`/api/step-runs/${stepRunId}/review`),

  /** Approve the run at `headSha`, the commit the person looked at: 409 once the branch moved.
   *  `pending` when the same person already approved that commit. */
  approveStep: (stepRunId: number, headSha: string) =>
    send<RequestResult>("POST", `/api/step-runs/${stepRunId}/approve`, { head_sha: headSha }),

  // --- runtimes: operator-owned, referenced by slug from a repo's process.yaml -------------------

  listRuntimes: () => request<Runtime[]>(orgPath("/runtimes")),

  createRuntime: (body: RuntimeIn) => send<Runtime>("POST", orgPath("/runtimes"), body),

  /** Tests the keys a create would store; nothing is stored. */
  checkRuntime: (body: RuntimeCheckIn) =>
    send<RuntimeCheckOut>("POST", orgPath("/runtimes/check"), body),

  /** Deployment-wide, so no org in the path. */
  runtimeOptions: () => request<RuntimeOptions>("/api/runtime-options"),

  /** Name and details only: slug and type are immutable, and the server answers 422 to either. */
  updateRuntime: (id: number, body: RuntimePatch) =>
    send<Runtime>("PATCH", `/api/runtimes/${id}`, body),

  /** 409 while a running, waiting or interrupted step_run still references it — the reason is
   *  in `detail`, and the page shows it as-is. */
  deleteRuntime: (id: number) => send<void>("DELETE", `/api/runtimes/${id}`),

  /** The projects whose latest process version names this runtime — the rows its
   *  `connected_projects` counts. */
  listRuntimeProjects: (id: number) => request<RuntimeProject[]>(`/api/runtimes/${id}/projects`),
  listRuntimeTemplates: (id: number) =>
    request<RuntimeTemplateBuild[]>(`/api/runtimes/${id}/templates`),

  /** Write-only, like a project secret: the value goes in and only the name comes back. */
  putRuntimeCredential: (id: number, name: string, value: string) =>
    send<{ name: string }>("PUT", `/api/runtimes/${id}/credentials/${encodeURIComponent(name)}`, {
      value,
    }),

  deleteRuntimeCredential: (id: number, name: string) =>
    send<void>("DELETE", `/api/runtimes/${id}/credentials/${encodeURIComponent(name)}`),

  /** The vendor's cheapest authenticated call with the stored credential; nothing is stored. */
  testRuntimeCredential: (id: number) =>
    send<CredentialTest>("POST", `/api/runtimes/${id}/test-credential`),

  /** Start `codex login --device-auth` on the host. 202 with the URL and the one-time code;
   *  409 for an api-key type, and for a claude subscription (no device flow: the token from
   *  `claude setup-token` is pasted instead). */
  startRuntimeLogin: (id: number) => send<RuntimeLogin>("POST", `/api/runtimes/${id}/login`),

  /** The session lives in the API process's memory, so it is polled at the API that started
   *  it — a restart between the two calls answers 404, which the page reads as "start again". */
  getRuntimeLoginStatus: (id: number, sessionId: string) =>
    request<RuntimeLoginStatus>(`/api/runtimes/${id}/login/${encodeURIComponent(sessionId)}`),

  /** The organization's whole factory, for the board on `/overview`: every project at once. */
  getOverview: (doneDays?: number) =>
    request<OverviewResponse>(orgPath(`/overview${query({ done_days: doneDays })}`)),

  // --- MCP access: the tokens an MCP client signs in with, and the tools they reach ---------------
  //
  // Each sends the cookie as well as any bearer, through `request` because `send` cannot: the API
  // mints as the Google session when there is one, so a person owns the row, not the shared token.

  listMcpTokens: () => request<McpTokenIndex>(orgPath("/mcp/tokens"), { credentials: "include" }),

  mintMcpToken: (body: McpTokenMintIn) =>
    request<McpTokenMinted>(orgPath("/mcp/tokens"), {
      method: "POST",
      body: JSON.stringify(body),
      credentials: "include",
    }),

  revokeMcpToken: (id: number) =>
    request<void>(`/api/mcp/tokens/${id}/revoke`, { method: "POST", credentials: "include" }),

  getVersion: () => request<Version>("/api/version", { credentials: "include" }),

  listMcpTools: () => request<McpToolCatalog>("/api/mcp/tools", { credentials: "include" }),

  // --- The assistant: its settings page ----------------------------------------------------------
  //
  // With the cookie, as the MCP routes are: a change records the signed-in person, not the shared
  // token, and the conversation a test makes is theirs.

  getAssistantSettings: () =>
    request<AssistantSettingsOut>(orgPath("/settings/assistant"), { credentials: "include" }),

  getAssistantReady: () =>
    request<AssistantReadyOut>(orgPath("/settings/assistant/ready"), { credentials: "include" }),

  saveAssistantSettings: (body: AssistantSettings) =>
    request<AssistantSettings>(orgPath("/settings/assistant"), {
      method: "PUT",
      body: JSON.stringify(body),
      credentials: "include",
    }),

  /** Refused with `settings_changed` unless the backend and auth mode are the saved ones. */
  setAssistantCredential: (backend: string, authMode: string, value: string) =>
    request<void>(orgPath(`/settings/assistant/credential/${backend}/${authMode}`), {
      method: "PUT",
      body: JSON.stringify({ value }),
      credentials: "include",
    }),

  clearAssistantCredential: (backend: string, authMode: string) =>
    request<void>(orgPath(`/settings/assistant/credential/${backend}/${authMode}`), {
      method: "DELETE",
      credentials: "include",
    }),

  /** One real turn in an archived conversation; a refusal's `detail` is the send path's. */
  testAssistant: () =>
    request<{ conversation_id: number }>(orgPath("/settings/assistant/test"), {
      method: "POST",
      credentials: "include",
    }),

  getAssistantItems: (conversationId: number) =>
    request<ChatItems>(`/api/assistant/conversations/${conversationId}/items`, {
      credentials: "include",
    }),

  // --- The assistant: its conversations, the Drawer's stream, and the proposals it decides ------
  //
  // With the cookie for the same reason: a conversation is its owner's, and every route answers
  // 404 for anyone else's.

  listConversations: () =>
    request<ChatConversation[]>(orgPath("/assistant/conversations"), { credentials: "include" }),

  createConversation: () =>
    request<ChatConversation>(orgPath("/assistant/conversations"), {
      method: "POST",
      body: JSON.stringify({}),
      credentials: "include",
    }),

  renameConversation: (conversationId: number, title: string) =>
    request<ChatConversation>(`/api/assistant/conversations/${conversationId}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
      credentials: "include",
    }),

  deleteConversation: (conversationId: number) =>
    request<void>(`/api/assistant/conversations/${conversationId}`, {
      method: "DELETE",
      credentials: "include",
    }),

  /** 202 with the queued turn. A refusal's `code` says which: `turn_live`, `not_configured`,
   *  `assistant_unavailable` or `daily_budget`. */
  sendAssistantMessage: (conversationId: number, body: { text: string; page: string | null }) =>
    request<{ turn_id: number }>(`/api/assistant/conversations/${conversationId}/messages`, {
      method: "POST",
      body: JSON.stringify(body),
      credentials: "include",
    }),

  cancelAssistantTurn: (turnId: number) =>
    request<void>(`/api/assistant/turns/${turnId}/cancel`, {
      method: "POST",
      credentials: "include",
    }),

  /** The conversation's server-sent events, as the body to read. `lastEventId` is the highest
   *  `rev` already held: the API resends every item written or rewritten since. */
  openConversationStream: async (
    conversationId: number,
    lastEventId: number,
    signal: AbortSignal,
  ): Promise<ReadableStream<Uint8Array>> => {
    const response = await call(`/api/assistant/conversations/${conversationId}/stream`, {
      headers: {
        Accept: "text/event-stream",
        ...(lastEventId > 0 ? { "Last-Event-ID": String(lastEventId) } : {}),
      },
      credentials: "include",
      signal,
    });
    if (!response.body) throw new ApiError(response.status, "the stream has no body");
    return response.body;
  },

  approveProposal: (proposalId: number) =>
    request<ProposalDecision>(`/api/assistant/proposals/${proposalId}/approve`, {
      method: "POST",
      credentials: "include",
    }),

  declineProposal: (proposalId: number) =>
    request<ProposalDecision>(`/api/assistant/proposals/${proposalId}/decline`, {
      method: "POST",
      credentials: "include",
    }),

  /** An `editor_draft` card's draft, for the process editor to load. */
  getAssistantDraft: (itemId: number) =>
    request<EditorDraftPayload>(`/api/assistant/drafts/${itemId}`, { credentials: "include" }),
};
