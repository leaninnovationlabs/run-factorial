/**
 * What state a ticket is in, in one vocabulary, derived in one place.
 *
 * The problem this solves: `TicketStatus` is coarser than `StepRunStatus`, and `blocked` is
 * where three unrelated endings land. An interrupted run, a gate rejected with no
 * `onReject` to loop back to, and an agent step that exhausted its retries all park the ticket
 * with `status = blocked` (`worker/agent_steps.py`, `worker/gates.py`, `worker/failures.py`), and
 * every surface that read `status` alone therefore called all three "blocked" while the attempt
 * beside it said `interrupted`. Worse, they disagreed about the colour: the same interrupted run
 * drew amber on its attempt tag, red in the header, and slate on the rail.
 *
 * So: `conditionOf` refines the ticket status with the latest attempt at the step the ticket is
 * on, and `CONDITION_TONES` is the ONE table every surface reads for the ink. `runTone` maps the
 * run statuses onto the same tones, so an attempt and the ticket it parked cannot disagree.
 *
 * Deliberately NOT a fifth `TicketStatus` in Postgres. `blocked` is the state machine's own word
 * and decision 26 leans on it: a parked ticket is released by the removal of a parking label, and
 * `worker/reconcile.py` tests `status == BLOCKED and issue.labels & PARKING_LABELS` as one branch
 * for both kinds of parking. The run status is already the discriminator `_resume_parked`
 * uses to tell a resume from an unblock, so this reads the same fact the orchestrator does rather
 * than inventing a second one.
 *
 * Also deliberately free of ADLC step names: like the factory board's lanes, a condition is
 * defined by the orchestrator rather than by any project's `process.yaml`, so it means the same
 * thing on a view that spans projects.
 */

import type { StepRunStatus, TicketStatus } from "./api/types";
import type { Glyph } from "./components/ui/icons";

/**
 * The state a reader is told a ticket is in.
 *
 * Finer than `TicketStatus` in two directions. `blocked` splits by why the ticket parked, because
 * "a human stopped this and means to continue" and "this failed until it gave up" ask different
 * things of the reader. `running` splits by run counts, because the factory board's lanes need
 * "taken in, nothing run yet" and "on a second attempt" and those are facts about `step_run`
 * rows rather than about the ticket row.
 *
 * `waiting_human` is the third reason: a session the loop parked after it sat idle at
 * its prompt (decision 56). Its run reads `interrupted` exactly like a human's stop, and only the
 * park's reason (`TicketListItem.park_reason`) says the ticket is waiting on a person rather than
 * stopped by one.
 *
 * `exited` is the status itself (decision 29): a ticket off the floor asks nothing of anyone, so
 * it takes the neutral ink and Done's lane.
 *
 * `quarantined` is not a status at all — it is stamped ON TOP OF whatever status the ticket
 * carried into it (decision 31), so `conditionOf` checks it before anything else: a ticket the
 * loop has given up on is out of the loop regardless of what the rest of the row says.
 */
export type TicketCondition =
  | "admitted"
  | "running"
  | "retrying"
  | "waiting_gate"
  | "waiting_human"
  | "interrupted"
  | "rejected"
  | "blocked"
  | "quarantined"
  | "done"
  | "exited";

/**
 * The ink a condition is drawn in, everywhere it is drawn.
 *
 * `attention` is the colour of "a person has to do something": a gate holding for a decision, an
 * agent waiting on an answer and a run a human stopped are the same request, made three times.
 * `rejected` keeps an ink of its own rather than folding into `error` — a rejection is a decision
 * somebody made, not a fault — and `neutral` is for states that ask nothing of anyone.
 */
export type ConditionTone = "neutral" | "active" | "attention" | "rejected" | "error" | "success";

export const CONDITION_TONES: Record<TicketCondition, ConditionTone> = {
  admitted: "neutral",
  running: "active",
  retrying: "active",
  waiting_gate: "attention",
  waiting_human: "attention",
  interrupted: "attention",
  rejected: "rejected",
  blocked: "error",
  quarantined: "error",
  done: "success",
  exited: "neutral",
};

/**
 * The ADS role each tone is drawn in (decision 50): neutral, information, warning, discovery,
 * danger, success. `rejected` takes discovery — purple, a family of its own — because ADS's accent
 * orange is the same ink as its warning text, and a rejection would read as a gate.
 */
export type Role = "neutral" | "information" | "warning" | "discovery" | "danger" | "success";

export const TONE_ROLE: Record<ConditionTone, Role> = {
  neutral: "neutral",
  active: "information",
  attention: "warning",
  rejected: "discovery",
  error: "danger",
  success: "success",
};

/*
 * A role on each kind of surface. The class strings are written out whole, never assembled, so
 * Tailwind's scanner finds every one; each subtle pair is at least 5.5:1.
 */

/** A lozenge or a notice: the role's wash under its ink. */
export const ROLE_SUBTLE: Record<Role, string> = {
  neutral: "bg-neutral text-subtle",
  information: "bg-information text-information",
  warning: "bg-warning text-warning",
  discovery: "bg-discovery text-discovery",
  danger: "bg-danger text-danger",
  success: "bg-success text-success",
};

/** A quiet lozenge: half the role's wash under neutral ink, for a page of many verdicts where
 *  full-strength red and green on every row shout over the one that matters. The role still
 *  shows in the wash and in the icon beside the words (`ROLE_ICON`). */
export const ROLE_QUIET: Record<Role, string> = {
  neutral: "bg-neutral text-subtle",
  information: "bg-information/50 text-subtle",
  warning: "bg-warning/50 text-subtle",
  discovery: "bg-discovery/50 text-subtle",
  danger: "bg-danger/50 text-subtle",
  success: "bg-success/50 text-subtle",
};

/** The wash alone, under default ink: a notice's ground, where the text is prose, not a label. */
export const ROLE_WASH: Record<Role, string> = {
  neutral: "bg-neutral",
  information: "bg-information",
  warning: "bg-warning",
  discovery: "bg-discovery",
  danger: "bg-danger",
  success: "bg-success",
};

/** The emphatic form: a bold fill with inverse ink. */
export const ROLE_BOLD: Record<Role, string> = {
  neutral: "bg-neutral-bold text-inverse",
  information: "bg-information-bold text-inverse",
  warning: "bg-warning-bold text-warning-inverse",
  discovery: "bg-discovery-bold text-inverse",
  danger: "bg-danger-bold text-inverse",
  success: "bg-success-bold text-inverse",
};

/** Bare ink, for a fact written in a row. */
export const ROLE_TEXT: Record<Role, string> = {
  neutral: "text-subtle",
  information: "text-information",
  warning: "text-warning",
  discovery: "text-discovery",
  danger: "text-danger",
  success: "text-success",
};

/** A dot or a mark in a chart — the activity rail, an event row. Never the only signal: the row
 *  beside it names the state in words. */
export const ROLE_DOT: Record<Role, string> = {
  neutral: "bg-chart-neutral",
  information: "bg-chart-information",
  warning: "bg-chart-warning",
  discovery: "bg-chart-discovery",
  danger: "bg-chart-danger",
  success: "bg-chart-success",
};

/** The icon a notice in each role carries (ADS content standard §6), in the role's icon ink. */
export const ROLE_ICON: Record<Role, { glyph: Glyph; ink: string }> = {
  neutral: { glyph: "status-information", ink: "text-icon-subtle" },
  information: { glyph: "status-information", ink: "text-icon-information" },
  warning: { glyph: "status-warning", ink: "text-icon-warning" },
  discovery: { glyph: "status-discovery", ink: "text-icon-discovery" },
  danger: { glyph: "status-error", ink: "text-icon-danger" },
  success: { glyph: "status-success", ink: "text-icon-success" },
};

/**
 * One condition, every surface. Most surfaces draw a condition in its tone's role; the board is
 * the exception twice over, on purpose, and says so in its lane legend:
 *
 * - `retrying` is information (blue) on a lozenge — work is in progress — but its own orange lane
 *   on the board (`--fb-retrying`, the chart's orange), where "on a second attempt" is the fact a
 *   reader scanning a column is looking for.
 * - `done` is success (green) on a lozenge, a verdict about one ticket; on the board it shares the
 *   neutral `admitted` lane colour, because between them they are most of the board and colouring
 *   both would spend the eye's attention on the two states nobody needs to act on.
 *
 * The flow graph paints only the tones that say something its shapes cannot (`NodeTone`).
 */
export const BOARD_LANE_TOKEN: Record<TicketCondition, string> = {
  admitted: "var(--ds-chart-neutral)",
  running: "var(--ds-chart-information)",
  retrying: "var(--ds-chart-orange-bold)",
  waiting_gate: "var(--ds-chart-yellow-bold)",
  waiting_human: "var(--ds-chart-yellow-bold)",
  interrupted: "var(--ds-chart-yellow-bold)",
  rejected: "var(--ds-chart-discovery)",
  blocked: "var(--ds-chart-danger)",
  quarantined: "var(--ds-chart-danger)",
  done: "var(--ds-chart-neutral)",
  exited: "var(--ds-chart-neutral)",
};

/**
 * The tones a process-graph node paints with, which are a subset.
 *
 * `active`, `neutral` and `success` are absent on purpose. A node in motion or finished cleanly
 * keeps the treatment the graph already gives it — the colour of its KIND while current, slate
 * once it is past — because the shape and the position say those things better than a colour
 * would. Only a tone that says something the graph cannot otherwise show earns an override, and
 * all three that do mean the same thing: this step did not simply run and pass.
 */
export type NodeTone = Extract<ConditionTone, "attention" | "rejected" | "error">;

export function isNodeTone(tone: ConditionTone): tone is NodeTone {
  return tone === "attention" || tone === "rejected" || tone === "error";
}

/**
 * The glossary: one name per condition, in sentence case (ADS content standard §13), used
 * wherever a condition is named. `LABEL_VARIANTS` lists the screens that name one differently, and
 * why; nothing else may.
 *
 * "Waiting" (`waiting_human`) mirrors the `factorial:waiting` label, and is not a gate run's
 * `waiting` (`RUN_LABELS`), which the ticket names "Waiting at gate".
 */
export const CONDITION_LABELS: Record<TicketCondition, string> = {
  admitted: "Admitted",
  running: "Running",
  retrying: "Retrying",
  waiting_gate: "Waiting at gate",
  waiting_human: "Waiting",
  interrupted: "Interrupted",
  rejected: "Rejected",
  blocked: "Blocked",
  quarantined: "Stopped after errors",
  done: "Done",
  exited: "Exited",
};

export const LABEL_VARIANTS = {
  /** The ticket list's status filter selects on `status`, and `blocked` holds waiting,
   *  interrupted and rejected tickets as well as escalations: "Stopped" names the set the filter
   *  returns, where "Blocked" over a row reading "Interrupted" would look like a mistake in one
   *  of the two. */
  statusFilter: { blocked: "Stopped" },
  /** The board's column headings are one word each, and a column is a lane, not a condition:
   *  "Gate" holds every ticket waiting on a person, "Error" blocked and quarantined ones. */
  boardLane: { waiting_gate: "Gate", blocked: "Error" },
} as const;

/**
 * What a run is drawn as: its status, except that a run a person forced past its failed
 * conditions (decision 78) is `forced`, and a run a redo set aside (decision 76) is `set_aside`,
 * whatever it ended as. A forced run reads `succeeded` on the wire, and every surface that shows a
 * run's outcome goes through this, so none of them draws it as a success, nor a set-aside run as
 * where the ticket stands.
 */
export type RunShown = StepRunStatus | "forced" | "set_aside";

export function runShown(run: {
  status: StepRunStatus;
  forced_by?: string | null;
  superseded_at?: string | null;
}): RunShown {
  if (run.superseded_at) return "set_aside";
  return run.forced_by ? "forced" : run.status;
}

/** A run status in the glossary's voice. */
export const RUN_LABELS: Record<RunShown, string> = {
  running: "Running",
  idle: "Idle",
  waiting: "Waiting",
  succeeded: "Succeeded",
  failed: "Failed",
  timeout: "Timed out",
  cancelled: "Canceled",
  interrupted: "Interrupted",
  approved: "Approved",
  rejected: "Rejected",
  forced: "Forced",
  set_aside: "Set aside",
};

/**
 * A run status in the same tones the conditions use.
 *
 * The pair is what keeps an attempt's tag and its ticket's header the same colour. `cancelled` is
 * neutral rather than an error: the run was called off, and whatever the ticket does next is
 * carried by the ticket's own condition, not by this row.
 */
export function runTone(status: RunShown | "info"): ConditionTone {
  switch (status) {
    case "succeeded":
    case "approved":
      return "success";
    case "failed":
    case "timeout":
      return "error";
    case "rejected":
      return "rejected";
    case "waiting":
    case "idle":
    case "interrupted":
    case "forced":
      return "attention";
    case "running":
      return "active";
    default:
      return "neutral";
  }
}

/** What `conditionOf` needs to know. Run counts are optional: only the board has them. */
export interface ConditionInput {
  status: TicketStatus;
  /** The status of the highest attempt at `current_step`, or null when it has none yet. */
  latestRunStatus: StepRunStatus | string | null;
  /** Why that run was parked (`TicketListItem.park_reason`): `idle` is waiting on a human.
   *  Optional: a caller without it reads every parked run as Interrupted, the older answer. */
  parkReason?: string | null;
  /** `step_run` rows across every step. Only 0 is load-bearing — "admitted, never run". */
  runs?: number;
  /** Attempt number at the current step, counted from the ticket's last restart
   *  (`OverviewTicket.attempt`). Over 1 is a retry. */
  attempt?: number | null;
  /** `quarantined_at !== null`. Checked first by `conditionOf` — see `TicketCondition`'s doc
   *  comment on why quarantine outranks everything else here. */
  quarantined?: boolean;
}

/**
 * The one derivation.
 *
 * `runs` and `attempt` are optional because the ticket page does not have them as aggregates and
 * does not need them: `running` is the honest answer there, and the finer split exists for the
 * board's lanes. A caller that omits them gets `running` rather than a wrong guess.
 *
 * A live `idle` run stays `running`, though once `agent_idle` names its spell it waits on a human
 * for at most `idle_park_after`: that window does not earn a second per-row event subquery, the
 * attempt's Idle tag carries it meanwhile, and GitHub already shows `factorial:waiting`.
 */
export function conditionOf(input: ConditionInput): TicketCondition {
  if (input.quarantined) return "quarantined";
  if (input.status === "done") return "done";
  if (input.status === "exited") return "exited";
  if (input.status === "waiting_gate") return "waiting_gate";
  if (input.status === "blocked") {
    // Null is a parked ticket whose `step_run` row has not arrived on this poll — the ticket and
    // the runs are separate queries. Parked is still the truth, so it falls through to `blocked`.
    if (input.latestRunStatus === "interrupted") {
      return input.parkReason === "idle" ? "waiting_human" : "interrupted";
    }
    if (input.latestRunStatus === "rejected") return "rejected";
    return "blocked";
  }
  if (input.runs === 0) return "admitted";
  return (input.attempt ?? 1) > 1 ? "retrying" : "running";
}

/** The highest attempt at `step`, the first seen on a tie, or null when nothing has run there. */
export function latestRunAt<R extends { step_name: string; attempt: number }>(
  runs: ReadonlyArray<R>,
  step: string,
): R | null {
  let best: R | null = null;
  for (const run of runs) {
    if (run.step_name !== step) continue;
    if (best === null || run.attempt > best.attempt) best = run;
  }
  return best;
}

/** The status of the highest attempt at `step`, or null when nothing has run there. */
export function latestRunStatusAt(
  runs: ReadonlyArray<{ step_name: string; attempt: number; status: string }>,
  step: string,
): string | null {
  return latestRunAt(runs, step)?.status ?? null;
}
