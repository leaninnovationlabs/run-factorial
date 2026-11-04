import type { StepRun, TicketDetail } from "../../api/types";
import { conditionCalls, type ProcessConfigLike } from "../flow";

/** The statuses the loop accepts a run from, by kind: `worker/completions.py::forceable`. */
const FORCEABLE: Record<string, ReadonlySet<string>> = {
  agent: new Set(["failed", "timeout", "cancelled", "interrupted", "idle"]),
  check: new Set(["failed", "timeout", "cancelled", "running"]),
};

const OPEN = new Set<string>(["running", "blocked", "waiting_gate"]);

export interface ForceOffer {
  /** The latest attempt at the ticket's step, which `POST /step-runs/{id}/complete` names. */
  run: StepRun;
  ticketId: number;
  /** Why the button is drawn but cannot act, written beside it; null when it can. */
  withheld: string | null;
}

/** What the step after a forced one waits on, from the pinned config. */
export interface NextNeeds {
  step: string;
  needs: string[];
}

/** `onOpenChange` lets the page hold the offer while its dialog is open: an accepted run moves
 *  the ticket on, which withdraws the offer, and the dialog showing the answer would go with it.
 *  `next` is shown beside the failures a person may continue past. */
export type ForceCompleteProps = ForceOffer & {
  onOpenChange?: (open: boolean) => void;
  next?: NextNeeds | null;
};

/** The next step's preconditions, or its gate's pull request, or null when it waits on
 *  neither or the config cannot say. */
export function nextNeeds(config: ProcessConfigLike | null, stepName: string): NextNeeds | null {
  const step = config?.steps.find((candidate) => candidate.name === stepName);
  const next = step && config?.steps.find((candidate) => candidate.name === step.next);
  if (!next) return null;
  const pr = next.gate?.pr;
  const needs =
    next.kind === "human_gate"
      ? pr?.head && pr.base
        ? [`a pull request from ${pr.head} into ${pr.base}`]
        : []
      : conditionCalls(next.preconditions);
  return needs.length > 0 ? { step: next.name, needs } : null;
}

/**
 * Force complete for the ticket's latest run, withheld with the reason in place where the API
 * would refuse it, or null where the control has no meaning (decision 78).
 */
export function forceOffer(
  ticket: Pick<TicketDetail, "id" | "status" | "current_step">,
  condition: string | null,
  latest: StepRun | null,
): ForceOffer | null {
  if (!latest || !OPEN.has(ticket.status)) return null;
  const offer = (withheld: string | null): ForceOffer => ({
    run: latest,
    ticketId: ticket.id,
    withheld,
  });
  if (condition === "quarantined") return offer("Requeue the ticket first.");
  if (latest.kind === "human_gate" && latest.status === "waiting")
    return offer("A gate step completes only through its review.");
  if (latest.kind === "action" && latest.status !== "succeeded")
    return offer("An action step has no expected results to check.");
  if (latest.kind === "agent" && latest.status === "running")
    return offer("The agent is mid-turn. Interrupt it first.");
  if (FORCEABLE[latest.kind]?.has(latest.status)) return offer(null);
  return null;
}
