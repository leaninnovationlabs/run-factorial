/**
 * The board's lanes and the node each ticket becomes. Split from `model.ts`, which re-exports it;
 * see there for why the lanes are ticket state and not ADLC steps.
 */

import type { TicketStatus } from "../../api/types";
import { conditionOf, type TicketCondition } from "../../status";

/**
 * The board's columns. Process-independent by construction — see the note above.
 *
 * `admitted` is the ticket the orchestrator has taken in but not yet run: `running` with no
 * `step_run` rows at all. It is a real and useful state to see, because a pile-up there means
 * admission is outrunning the workers.
 */
export type Lane = "admitted" | "running" | "gate" | "error" | "retrying" | "done";

export const LANES: Lane[] = ["admitted", "running", "gate", "error", "retrying", "done"];

export const LANE_LABELS: Record<Lane, string> = {
  admitted: "Admitted",
  running: "Running",
  gate: "Gate",
  error: "Error",
  retrying: "Retrying",
  done: "Done",
};

export const LANE_HINTS: Record<Lane, string> = {
  admitted: "Admitted, nothing has run yet",
  running: "Working, first attempt at this step",
  gate: "Waiting for a person's decision",
  error: "Stopped on a failure",
  retrying: "Working again after a failed attempt",
  done: "Done or exited",
};

/**
 * One ticket, as the board draws it.
 */
export interface IssueNode {
  /** The ticket id. Also the row's link target, `/tickets/<id>`. */
  id: number;
  /** `OverviewTicket.external_key` — usually the bare issue number; see `issueNumber`. */
  key: string;
  /**
   * The issue title the orchestrator last observed, or null when it has none stored.
   *
   * The loop reads the whole issue every tick to evaluate predicates, so keeping the title costs
   * a column and no extra GitHub call. Null is a ticket that has not ticked since it shipped —
   * including anything already done — and those rows show their number alone.
   */
  title: string | null;
  projectId: number;
  /** `current_step`. Shown in the tooltip; deliberately NOT what decides the lane. */
  step: string;
  /**
   * Which ADLC process the ticket pinned at admission. Shown beside the step, because with
   * several processes per project a step name is no longer unique even within one project.
   *
   * Deliberately NOT a band: `layout` bands on project and lanes on status, and step names being
   * unstable across processes is the same reason those bands do not key on steps either.
   *
   * Optional for the same reason as `quarantined`: the board's fixtures predate it.
   */
  process?: string;
  status: TicketStatus;
  /**
   * `OverviewTicket.quarantined_at !== null`, collapsed to a flag: the lane cares only whether
   * the loop has stopped leasing this ticket, not when. Orthogonal to `status` — a quarantined
   * ticket still carries whatever status it had when its tick started failing.
   *
   * Optional, like `ConditionInput.quarantined`: most of this board's fixtures predate
   * quarantine and have no opinion on it, and absent should read as false, not as a type error
   * on every node literal that has never heard of the flag.
   */
  quarantined?: boolean;
  /**
   * The status of the highest attempt at `step`, or null when it has none — `OverviewTicket
   * .latest_run_status`. It is what tells an interrupted ticket from an escalated one, since both
   * park with `status = blocked`; see `status.ts`.
   */
  latestRunStatus: string | null;
  /**
   * `OverviewTicket.park_reason`. It never changes the lane — Waiting and Interrupted are both
   * Gate — and is carried so `laneOf` reads the same input as every other `conditionOf` caller.
   * Optional like `quarantined`.
   */
  parkReason?: string | null;
  /**
   * `OverviewTicket.runs`. Only the 0 case is load-bearing here: 0 is exactly "admitted but
   * never run". See `attempt` below for why this is not the retry signal.
   */
  runs: number;
  /**
   * The in-flight run's attempt number at the CURRENT step, counted from the ticket's last
   * restart, or null when nothing is running. Stored attempt numbers keep counting across a
   * restart; the server re-bases this one, so the first run after a restart is no retry.
   *
   * The honest retry signal, and why `runs` is not one: `runs > 1` is true of every ticket that
   * has merely advanced a step. `OverviewTicket.attempt` carries this.
   */
  attempt: number | null;
  /**
   * What this ticket has spent so far, summed over every `step_run` it owns — dollars, wall-clock
   * minutes of agent runtime, and tokens. Totals, not rates: the question a shade mode answers
   * is "which tickets are eating the budget", and that is cumulative.
   *
   * A ticket with no runs has three zeroes, which is the truth rather than a gap.
   */
  costUsd: number;
  minutes: number;
  tokens: number;
  /**
   * Whether any of the ticket's runs was priced at list rather than measured
   * (`OverviewTicket.cost_estimated`). Optional like `quarantined`, and for the same reason:
   * the fixtures predate it, and absent reads as false.
   */
  costEstimated?: boolean;
}

export interface ProjectBand {
  id: number;
  name: string;
}

/**
 * The lane each condition falls in.
 *
 * Gate is "waiting on a human decision", which an interrupted run is exactly: somebody stopped it
 * and it will not move until they release it. A session parked idle waits on a person's answer,
 * so it is Gate too. Filing it under Error would put it beside the
 * escalations, where it reads as something that broke rather than as something waiting on the
 * person who is looking at the board.
 *
 * Exited sits with Done: both have left the floor, and the board's lanes are what the factory is
 * doing.
 *
 * Quarantined sits with Error: the loop has given up on the ticket, which is exactly what Error
 * means, whatever status it carried into quarantine.
 */
const LANE_OF_CONDITION: Record<TicketCondition, Lane> = {
  admitted: "admitted",
  running: "running",
  retrying: "retrying",
  waiting_gate: "gate",
  waiting_human: "gate",
  interrupted: "gate",
  rejected: "error",
  blocked: "error",
  quarantined: "error",
  done: "done",
  exited: "done",
};

/**
 * The lane a ticket belongs to. Total: every ticket lands in exactly one, which is what lets the
 * board claim to show the whole factory.
 */
export function laneOf(node: IssueNode): Lane {
  return LANE_OF_CONDITION[
    conditionOf({
      status: node.status,
      latestRunStatus: node.latestRunStatus,
      parkReason: node.parkReason,
      runs: node.runs,
      attempt: node.attempt,
      quarantined: node.quarantined,
    })
  ];
}

/** Retries at the current step — 0 for a first attempt, and the input to the retry ramp. */
export function retriesOf(node: IssueNode): number {
  return Math.max(0, (node.attempt ?? 1) - 1);
}

export function countLanes(nodes: IssueNode[]): Record<Lane, number> {
  const counts: Record<Lane, number> = {
    admitted: 0,
    running: 0,
    gate: 0,
    error: 0,
    retrying: 0,
    done: 0,
  };
  for (const node of nodes) counts[laneOf(node)] += 1;
  return counts;
}
