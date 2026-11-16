/** The words a row is written in: an event's label, the ways out and back, and the snippet a
 *  long fact is cut to. `facts.ts` decides which of them a row shows. */

/** The first paragraph, capped — enough to tell two summaries apart, never enough to fill a row. */
export function snippet(text: string, max = 120): string {
  const firstLine =
    text
      .trim()
      .split(/\n\s*\n/, 1)[0]
      ?.replace(/\s+/g, " ")
      .trim() ?? "";
  if (firstLine.length <= max) return firstLine;
  return `${firstLine.slice(0, max - 1).trimEnd()}…`;
}

export const HUMANISED: Record<string, string> = {
  ticket_admitted: "Admitted",
  step_entered: "Entered step",
  step_started: "Step started",
  agent_spawned: "Agent started",
  agent_completed: "Agent completed",
  agent_failed: "Agent failed",
  postcondition_failed: "Expected results missing",
  gate_waiting: "Waiting for review",
  gate_approved: "Approved",
  gate_rejected: "Changes requested",
  gate_merge_refused: "Merge refused",
  gate_review_ignored: "Review not counted",
  gate_pr_closed: "Pull request closed",
  step_completed: "Step completed",
  retry_scheduled: "Retry scheduled",
  escalated: "Escalated",
  spawn_refused: "Spawn refused",
  ticket_quarantined: "Stopped after errors",
  substrate_unavailable: "Sandbox service unavailable",
  ticket_requeued: "Requeued",
  ticket_done: "Done",
  unblocked: "Unblocked",
  unblock_requested: "Unblock requested",
  unblock_refused: "Unblock refused",
  interrupt_requested: "Interrupt requested",
  retry_requested: "Retry now requested",
  agent_interrupted: "Interrupted",
  agent_parked: "Paused",
  interrupt_overtaken: "Interrupt not applied",
  agent_resumed: "Resumed",
  session_resume_requested: "Resume requested",
  session_resume_refused: "Resume refused",
  session_resume_skipped: "New session",
  terminal_attached: "Terminal attached",
  terminal_detached: "Terminal detached",
  terminal_input: "Typed in the terminal",
  terminal_seat_taken: "Typing taken over",
  inspect_requested: "Session requested",
  inspect_started: "Session opened",
  inspect_close_requested: "Session close requested",
  inspect_parked: "Session closed",
  exit_requested: "Exit requested",
  ticket_exited: "Exited",
  reentry_requested: "Re-entry requested",
  ticket_reentered: "Re-entered",
  reentry_refused: "Re-entry refused",
  restart_requested: "Restart requested",
  ticket_restarted: "Restarted",
  restart_refused: "Restart refused",
  preview_url_updated: "Preview published",
  preview_requested: "Preview requested",
  preview_stop_requested: "Preview stop requested",
  preview_started: "Preview started",
  preview_failed: "Preview failed",
  preview_stopped: "Preview stopped",
  preview_kept: "Preview kept running",
  preview_page_observed: "Page observed",
  run_outputs_observed: "Outputs recorded",
  step_changes_requested: "Change request sent",
  step_changes_refused: "Change request refused",
  completion_requested: "Force complete requested",
  completion_refused: "Not completed",
  step_accepted_on_request: "Accepted on request",
  step_forced: "Forced complete",
  step_approved: "Step approved",
  next_step_waiting: "Next step waiting",
  step_retry_requested: "Retry requested",
  step_retry_refused: "Retry refused",
  redo_requested: "Redo requested",
  step_redone: "Redone",
  redo_refused: "Redo refused",
  action_executed: "Action ran",
  action_failed: "Action failed",
  check_failed: "Check failed",
  admission_refused: "Not admitted",
  issue_create_requested: "Issue requested",
  issue_created: "Issue opened",
  shadow_accepted: "Accepted without running",
  dry_run_write_suppressed: "Skipped (dry run)",
  webhook_received: "GitHub update",
};

/** How a run's transcript says its agent's session ended (`api/activity.py`): Claude's
 *  `result.subtype`, or codex's own terminal signal. An unknown one shows as it came. */
const OUTCOMES: Record<string, string> = {
  success: "Agent finished",
  finished: "Agent finished",
  error: "Stopped: error",
  error_max_turns: "Stopped: turn limit reached",
  error_during_execution: "Stopped: error during the run",
  error_max_budget_usd: "Stopped: budget limit reached",
};

export function outcomeWords(outcome: string): string {
  return OUTCOMES[outcome] ?? outcome;
}

/** `run_outputs_observed` reads by what the worker found (decision 71). */
export const OUTPUT_LABELS: Record<string, string> = {
  observed: "Outputs recorded",
  no_branch: "Branch not found",
  ambiguous: "More than one file matches",
  lookup_failed: "Couldn't read files",
};

/** `preview_page_observed` reads by what the worker found (decision 58). */
export const PAGE_LABELS: Record<string, string> = {
  published: "Page published",
  missing: "No page published",
  ambiguous: "Page ambiguous",
  not_republished: "Page not republished",
  lookup_failed: "Page lookup failed",
};

/**
 * What a resumed agent was told, from `agent_resumed`'s by-reference record of it.
 *
 * The worker logs the count and the authors rather than the comment bodies, so that untrusted
 * ticket text — or a credential somebody pasted — never reaches the event store
 * (`worker/reconcile.py`). This reads it back in the same spirit: who steered it, not what they
 * said, which is on the issue for anyone who wants it.
 */
export function steering(count: unknown, authors: unknown): string | null {
  const n = typeof count === "number" ? count : 0;
  if (n === 0) return "no new instructions";
  const named = Array.isArray(authors) ? authors.filter((a) => typeof a === "string") : [];
  const comments = `${n} ${n === 1 ? "comment" : "comments"}`;
  return named.length > 0 ? `${comments} from ${named.join(", ")}` : comments;
}

/** Keys that say WHERE an event sits rather than WHAT happened; never the primary fact. */
export const POSITIONAL = new Set(["step", "attempt", "kind", "external_key", "process_version"]);

/** The surface a request came through, in the worker's words (`events/types.py`'s
 *  `REQUESTED_VIA_WORDS`). */
export const SURFACES: Record<string, string> = {
  dashboard: "from the dashboard",
  mcp: "through the Factorial MCP server",
  chat: "from the assistant",
};

/** The exit comment's own words for each way in (`worker/exits.py::_EXIT_CAUSES`). */
export const EXIT_CAUSES: Record<string, string> = {
  ...SURFACES,
  label: "the factorial:exit label was added",
  issue_closed: "the issue was closed",
};

export const REFUSAL_REASONS: Record<string, string> = {
  issue_closed: "the issue is closed; reopen it first",
  no_retry_policy: "this step's process has no retry rule",
  parked: "the session is paused; press Resume",
  stale: "the ticket escalated again after the request",
  no_default: "no default process, and the issue names none",
  unknown_process: "the issue's process label names no process this project offers",
  ambiguous_process: "the issue carries two process labels",
  lane_retired: "this process was removed, so there is nothing to restart on",
  merged_outside: "the ticket's work is already merged; restart again and confirm it",
  cancel_failed: "the run in flight couldn't be confirmed stopped; exit, then restart",
  exited: "the ticket exited first; restart it from there",
};

/** Why a Force complete was refused (`worker/completions.py::CompletionRefusal`). Apart from
 *  `REFUSAL_REASONS`, whose `stale` and `exited` are worded for un-block and restart. */
export const COMPLETION_REFUSALS: Record<string, string> = {
  stale: "a newer attempt started, or the ticket moved on",
  issue_closed: "the issue is closed; reopen it first",
  not_forceable: "only agent and check steps can be forced",
  mid_turn: "the agent is mid-turn; interrupt it first",
  release_pending: "a Resume or Unblock request is still pending",
  unmet: "its expected results aren't met",
  changed: "the failures changed since you read them",
  unmoved: "the rework hasn't moved the pull request its gate rejected",
  unavailable: "GitHub couldn't be read to check the expected results",
  exited: "the ticket exited first",
  redone: "the step was redone",
};

/** Why a Redo was refused (`worker/redos.py::RedoRefusal`). Apart from `REFUSAL_REASONS`, whose
 *  `stale` and `exited` are worded for un-block and restart. */
export const REDO_REFUSALS: Record<string, string> = {
  issue_closed: "the issue is closed; reopen it first",
  exited: "the ticket exited before the redo was answered",
  stale: "the ticket moved on before the redo was answered",
  cancel_failed: "the run in flight couldn't be confirmed stopped; nothing changed",
};

/** Why a Retry was refused (`worker/retries.py::RetryRefusal`). Apart from `REFUSAL_REASONS`,
 *  whose `stale` and `exited` are worded for un-block and restart. */
export const RETRY_REFUSALS: Record<string, string> = {
  stale: "the ticket moved on before the retry started",
  gate: "a gate is not retried",
  not_escalated: "the ticket is blocked on something else; Unblock asks again",
  off_the_floor: "the ticket is not running",
  preconditions_unmet: "the step can't start yet",
  spawn_refused: "the runtime can't run a session now",
  spawn_failed: "the runtime failed to start the attempt; the ticket is as it was",
  unavailable: "GitHub couldn't be read to check the preconditions",
};

/** Why a preview stopped, where the runtime records a code (`runtime.contract.PREVIEW_*`). Any
 *  other reason is a sentence the loop wrote, shown as it is. */
export const PREVIEW_STOPS: Record<string, string> = {
  idle: "Stopped after it sat idle.",
  renewal: "Stopped while its sandbox was renewed.",
  continued: "A later attempt continues in this workspace.",
};

/** A preview stop's reason in words, and what followed its code: a renewal that failed records
 *  `renewal` and then why, and the why is kept for Details. */
export function previewStop(reason: string): { said: string; more: string | null } {
  const [code = "", ...rest] = reason.split("\n");
  const said = PREVIEW_STOPS[code];
  if (said === undefined) return { said: reason, more: null };
  const more = rest.join("\n").trim();
  return { said, more: more || null };
}

/** A clause as a sentence: a capital first letter and a closing full stop. */
export function asSentence(text: string): string {
  const trimmed = text.trim();
  const capital = trimmed.charAt(0).toUpperCase() + trimmed.slice(1);
  return /[.!?]$/.test(capital) ? capital : `${capital}.`;
}

/** Why a Resume was refused (`worker/interrupts.py::ResumeRefusal`). A refusal an exception or
 *  the runtime words is stored as that text, and an older event's reason is a sentence: both
 *  show as they are. */
export const RESUME_REFUSALS: Record<string, string> = {
  not_parked: "the run it named is no longer the paused one",
  no_session: "this run never started a session, so there is none to continue",
  no_attachments: "this runtime can't pass on the images in the comments",
  no_agent_source: "this agent's settings couldn't be loaded",
};

/** Why an open session closed or was refused (`worker/inspect.py::ViewClosed`). */
const VIEW_CLOSED: Record<string, string> = {
  terminal_off: "the terminal is switched off on this installation",
  quarantined: "the ticket stopped after repeated errors; requeue it first",
  request_expired: "the session couldn't be opened in time",
  released: "this run's files were cleaned up",
  live: "the agent is still running in this workspace",
  continued: "a later attempt continues in this workspace; open the session on that attempt",
  too_many_awake: "too many previews and open sessions are already running; stop one first",
  no_credential: "no model credential is stored for this run's runtime; store one on its page",
  unsupported: "this run's runtime can't open a session",
  no_session: "this run never started, so there is no session to open",
  unreachable: "the sandbox couldn't be reached",
  gone: "this run's files were cleaned up",
  closed: "someone closed it",
  start_failed: "the session failed to start",
  ended_before_open: "the session ended before it opened",
  ended: "the session ended",
  view_failed: "the session view failed",
  limit: "it reached its time limit",
  unwatched: "nobody was watching it",
  overdue: "it passed its time limit, and nothing could reach it to stop it",
};

/** A span of seconds as a person says it: `55 minutes`, `1 minute`, `90 seconds`. */
function span(seconds: unknown): string | null {
  if (typeof seconds !== "number" || seconds <= 0) return null;
  const [count, unit] = seconds % 60 === 0 ? [seconds / 60, "minute"] : [seconds, "second"];
  return `${count} ${unit}${count === 1 ? "" : "s"}`;
}

/** `ViewClosed` as a clause, with the parameters its event carries when the caller has them.
 *  Any other reason is a sentence the runtime or an older event wrote, shown as it is. */
export function viewClosed(reason: string, details: Record<string, unknown> = {}): string {
  const by = typeof details.closed_by === "string" ? details.closed_by : null;
  if (reason === "closed" && by) return `${by} closed it`;
  const limit = span(details.limit_seconds);
  if (reason === "limit" && limit) return `it reached its time limit of ${limit}`;
  const idle = span(details.after_seconds);
  if (reason === "unwatched" && idle) return `nobody watched it for ${idle}`;
  if (reason === "request_expired" && idle) return `the session couldn't be opened within ${idle}`;
  const cap = typeof details.limit === "number" ? details.limit : null;
  if (reason === "too_many_awake" && cap) {
    return `${cap} previews and open sessions are already running; stop one first`;
  }
  return VIEW_CLOSED[reason] ?? reason;
}

/** Why a change request was refused (`worker/gates.py::ChangesRefusal`), naming the gate and
 *  the reviewed step where the caller knows them. */
export function changesRefused(
  reason: string,
  gate?: string | null,
  reviewed?: string | null,
): string {
  switch (reason) {
    case "no_pull_request":
      return "no pull request was found for this review";
    case "already_merged":
      return "the pull request is already merged";
    case "not_sent_back":
      return `${gate ?? "this review"} can't send work back to ${reviewed ?? "that step"}`;
    case "newer_attempt":
      return `a newer attempt of ${reviewed ?? "this step"} exists; review that one instead`;
    case "redone":
      return "the step was redone";
    default:
      return reason;
  }
}

/** Why a rework started a fresh session (`SESSION_RESUME_SKIPPED.reason`, `worker/rework.py`). */
export const FRESH_SESSION_REASONS: Record<string, string> = {
  not_interactive: "the previous attempt ran without a session",
  runtime_changed: "the runtime changed",
  cli_version_changed: "the CLI version changed",
  past_retention: "the previous attempt's files were cleaned up",
  no_session: "the previous attempt left no session",
  not_kept: "the workspace was not kept",
  no_branch: "the previous attempt held no branch",
};
