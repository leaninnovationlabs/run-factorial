import type { StepRun, TicketDetail, TimelineEntry } from "../../api/types";
import { latestRunAt } from "../../status";
import { newest, str } from "./details";

/** The rows that count down a failed run's backoff (`facts.ts`'s `dueAt`). */
const DUE_ROWS = new Set(["retry_scheduled", "unblocked", "ticket_reentered"]);

/** The run statuses a retry follows: `worker/tick_context.py::_FAILED_STATUSES`. */
const RETRIED = new Set<string>(["failed", "timeout", "cancelled"]);

/** The rows that block a ticket, newest deciding why: `worker/agent_steps.py::_BLOCKED_BY`. */
const BLOCKERS = new Set([
  "spawn_refused",
  "step_started",
  "escalated",
  "agent_parked",
  "agent_interrupted",
]);

/** The rows that cancel by step alone, with their words: "<first> <person>", else the second. */
const STEP_CANCELS = new Map<string, [string, string]>([
  ["step_accepted_on_request", ["accepted by", "accepted on request"]],
  ["step_forced", ["forced complete by", "forced complete"]],
  ["step_redone", ["redone by", "redone"]],
]);

export interface PendingRetry {
  /** The failed run a Retry follows, which `POST /step-runs/{id}/retry-step` names. */
  run: StepRun;
  /** When the backoff would start the next attempt anyway; null on an escalated step. */
  due: string | null;
}

export type RetryOffer = PendingRetry & { ticketId: number };

/**
 * The failed run a Retry would follow, or null: the latest run at the ticket's step failed, and
 * the ticket either waits out its backoff or escalated on that very failure. A ticket blocked
 * after the failure by something else, such as a refused spawn, is not an escalation, and the
 * loop refuses a Retry there (`worker/retries.py::escalated_on`).
 */
export function retryOffer(
  ticket: Pick<TicketDetail, "current_step" | "status">,
  runs: readonly StepRun[],
  entries: readonly TimelineEntry[],
): PendingRetry | null {
  const latest = latestRunAt(runs, ticket.current_step);
  if (!latest || !RETRIED.has(latest.status) || latest.kind === "human_gate") return null;
  if (ticket.status === "running") {
    const booking = newest(
      entries,
      (entry) =>
        DUE_ROWS.has(entry.type) &&
        entry.details.step === ticket.current_step &&
        entry.details.next_attempt === latest.attempt + 1 &&
        typeof entry.details.due_at === "string",
    );
    return { run: latest, due: booking ? str(booking.details.due_at) : null };
  }
  if (ticket.status === "blocked") {
    const blocker = newest(entries, (entry) => BLOCKERS.has(entry.type));
    const escalation =
      blocker?.type === "escalated" &&
      blocker.details.step === ticket.current_step &&
      blocker.details.attempt === latest.attempt;
    return escalation ? { run: latest, due: null } : null;
  }
  return null;
}

/**
 * The rows whose countdown a person overtook, each with what did it, before the attempt the row
 * books started: a Retry for that attempt the loop did not refuse, a Force complete, an accept
 * on request or a redo at its step, a restart or an exit. The row would otherwise count on toward a retry
 * the loop is no longer going to make. An attempt that started on its own redeems the row, and
 * nothing after that cancels it.
 */
export function cancelledRetries(entries: readonly TimelineEntry[]): Map<number, string> {
  const log = [...entries].sort((a, b) => a.id - b.id);
  const cancelled = new Map<number, string>();
  // A refused Retry left the backoff as it was, so its countdown runs on.
  const refused = new Set(
    log.filter((row) => row.type === "step_retry_refused").map((row) => row.details.request_id),
  );
  log.forEach((row, index) => {
    if (!DUE_ROWS.has(row.type) || typeof row.details.due_at !== "string") return;
    const { step, next_attempt: attempt } = row.details;
    for (const later of log.slice(index + 1)) {
      const d = later.details;
      const booked = d.step === step && (d.next_attempt ?? d.attempt) === attempt;
      const by = str(d.requested_by);
      if (later.type === "step_started" && booked) return;
      const retried =
        later.type === "retry_requested" ||
        (later.type === "step_retry_requested" && !refused.has(later.id));
      if (retried && booked) {
        cancelled.set(row.id, by ? `retried now by ${by}` : "retried now");
        return;
      }
      // These name the failed attempt, not the one the row books: the step alone matches.
      const stepWords = STEP_CANCELS.get(later.type);
      if (stepWords && d.step === step) {
        cancelled.set(row.id, by ? `${stepWords[0]} ${by}` : stepWords[1]);
        return;
      }
      if (later.type === "ticket_restarted") {
        cancelled.set(row.id, by ? `restarted by ${by}` : "restarted");
        return;
      }
      if (later.type === "ticket_exited") {
        cancelled.set(row.id, "the ticket exited");
        return;
      }
    }
  });
  return cancelled;
}
