/**
 * The query keys, one factory for every read, the live poll interval and the retry rule.
 *
 * A read the project owns sits under `keys.project(id)`, so a write that invalidates the project
 * refreshes it with no list of its own to keep. The pinned versions' reads are the one deliberate
 * exception (`processVersion` below).
 */

import { ApiError } from "./client";
import type { Span, TicketStatus } from "./types";

/** Live views. The reconciliation loop ticks every 30s by default. */
export const LIVE_MS = 10_000;

export const keys = {
  /** Every organization, for the shell's check of the URL's slug and the name in the top bar. */
  organizations: ["organizations"] as const,
  /** The page's organization with the reader's permissions in it. */
  organization: ["organization"] as const,
  /** What the deployment's sign-in offers; multi-org says itself here. */
  authProviders: ["auth-providers"] as const,
  roles: ["roles"] as const,
  permissions: ["permissions"] as const,
  members: ["members"] as const,
  invites: ["invites"] as const,
  projects: ["projects"] as const,
  project: (id: number) => ["projects", id] as const,
  githubConnection: (id: number) => ["projects", id, "github-connection"] as const,
  unknownHead: (id: number) => ["projects", id, "unknown-head"] as const,
  projectReadiness: (id: number) => ["projects", id, "readiness"] as const,
  pendingIssues: (id: number) => ["projects", id, "pending"] as const,
  tickets: (id: number, status?: TicketStatus, openOnly?: boolean) =>
    ["projects", id, "tickets", status ?? "all", openOnly ?? false] as const,
  board: (id: number) => ["projects", id, "board"] as const,
  /** By span, so a switch back to one the cache holds draws at once; under the project's prefix,
   *  so activating or syncing the project drops it with the rest. */
  dashboard: (id: number, span: Span) => ["projects", id, "dashboard", span] as const,
  /** Derived from the pinned config, so a sync that pins a new version changes it — and clears a
   *  cached "no config yet" 409. */
  projectLabels: (id: number) => ["projects", id, "labels"] as const,
  /** A pinned version's reads, by the version ROW's id: two lanes may both be at v1, so the
   *  number alone would serve one lane's snapshot for the other's. Outside the project's prefix
   *  on purpose — a version never changes, most of these cost GitHub reads, and a sync or a
   *  rename must not fetch them again. */
  processVersion: (id: number, versionId?: number) => ["process-versions", id, versionId] as const,
  processVersionRaw: (id: number, versionId?: number) =>
    ["process-versions", id, versionId, "raw"] as const,
  processVersionAgents: (id: number, versionId?: number) =>
    ["process-versions", id, versionId, "agents"] as const,
  configFiles: (id: number, versionId?: number) =>
    ["process-versions", id, versionId, "files"] as const,
  configFile: (id: number, versionId: number | undefined, path: string | null) =>
    ["process-versions", id, versionId, "files", path] as const,
  processTemplates: ["process-templates"] as const,
  ticket: (ticketId: number) => ["tickets", ticketId] as const,
  timeline: (ticketId: number) => ["tickets", ticketId, "timeline"] as const,
  /** By the pinned version as well as the ticket: a snapshot never changes, but a restart moves
   *  the ticket onto another, and the old one's answer is cached for ever (`useTicketProcess`). */
  ticketProcess: (ticketId: number, versionId: number | undefined) =>
    ["tickets", ticketId, "process", versionId] as const,
  restartPreview: (ticketId: number) => ["tickets", ticketId, "restart-preview"] as const,
  ticketIssue: (ticketId: number) => ["tickets", ticketId, "issue"] as const,
  gatePullRequest: (ticketId: number) => ["tickets", ticketId, "gate-pull-request"] as const,
  runActivity: (stepRunId: number) => ["step-runs", stepRunId, "activity"] as const,
  runPreview: (stepRunId: number) => ["step-runs", stepRunId, "preview"] as const,
  runOutputs: (stepRunId: number) => ["step-runs", stepRunId, "outputs"] as const,
  runTerminal: (stepRunId: number) => ["step-runs", stepRunId, "terminal"] as const,
  completion: (stepRunId: number, requestId: number) =>
    ["step-runs", stepRunId, "completion", requestId] as const,
  overview: ["overview"] as const,
  runtimes: ["runtimes"] as const,
  runtimeOptions: ["runtime-options"] as const,
  runtimeProjects: (id: number) => ["runtimes", id, "projects"] as const,
  runtimeTemplates: (id: number) => ["runtimes", id, "templates"] as const,
  /** Keyed by session as well as runtime: a second "Connect" starts a new session, and the old
   *  one's last answer must not be what the new code is shown against. */
  runtimeLogin: (id: number, sessionId: string) => ["runtimes", id, "login", sessionId] as const,
  /** Keyed on the exact text previewed, not just the project: editing the draft must never show
   *  a stale answer under a fresh-looking key, and reverting to a previously-seen text is free. */
  configPreview: (id: number, content: string) =>
    ["projects", id, "config-preview", content] as const,
  /** Under the project, so a seed's invalidation drops it: once the branch exists, the gallery
   *  stops offering the copy. */
  defaultBranchConfig: (id: number) => ["projects", id, "default-branch-config"] as const,
  mcpTokens: ["mcp", "tokens"] as const,
  mcpTools: ["mcp", "tools"] as const,
  version: ["version"] as const,
  assistantSettings: ["settings", "assistant"] as const,
  /** Under `assistantSettings`, so a change to the settings refreshes it too. */
  assistantReady: ["settings", "assistant", "ready"] as const,
  conversations: ["assistant", "conversations"] as const,
  /** An editor card's draft never changes: its item is written once. */
  assistantDraft: (itemId: number) => ["assistant", "drafts", itemId] as const,
};

/**
 * Retry transport faults, never the server's considered answer. A 410 for a purged run and a 404
 * for a step that never existed are both final: asking three more times changes nothing and just
 * delays the message the viewer needs.
 */
export function retryUnlessClientError(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false;
  return failureCount < 3;
}
