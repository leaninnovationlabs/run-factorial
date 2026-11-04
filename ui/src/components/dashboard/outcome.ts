import type { TicketOutcome } from "../../api/types";
import { CONDITION_LABELS } from "../../status";
import type { DashLook } from "./looks";

/** In the order the read model serves them, so every figure keys the three alike. */
export const OUTCOMES: readonly TicketOutcome[] = ["done", "open", "exited"];

export const OUTCOME_LABELS: Record<TicketOutcome, string> = {
  done: CONDITION_LABELS.done,
  open: "Open",
  exited: CONDITION_LABELS.exited,
};

export const outcomeLook = (outcome: TicketOutcome): DashLook => `outcome-${outcome}`;

/** `done`, `exited`, or else `open`: the rule the read model applies, repeated for a status. */
export function outcomeOf(status: string): TicketOutcome {
  return status === "done" || status === "exited" ? status : "open";
}
