import type { StepRunStatus, TicketDetail } from "../../api/types";
import {
  CONDITION_TONES,
  conditionOf,
  isNodeTone,
  latestRunStatusAt,
  type NodeTone,
  type RunShown,
  runShown,
  runTone,
} from "../../status";
import { ENTRY_NODE_ID } from "./processGraph";

/** Steps that have already happened on a ticket: admission, any run, and the current step. */
export function visitedNodeIds(
  runs: ReadonlyArray<{ step_name: string }>,
  currentStep?: string,
): Set<string> {
  const ids = new Set<string>([ENTRY_NODE_ID]);
  for (const run of runs) ids.add(run.step_name);
  if (currentStep) ids.add(currentStep);
  return ids;
}

/** Highest attempt recorded for each step name. */
export function attemptCountsFromRuns(
  runs: ReadonlyArray<{ step_name: string; attempt: number }>,
): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const run of runs) {
    counts[run.step_name] = Math.max(counts[run.step_name] ?? 0, run.attempt);
  }
  return counts;
}

/**
 * What each node's badge under the graph says about attempts.
 *
 * A step that has run says how many times — the badge's standing job, and the signal that a step
 * is in trouble. The step the reader is IN says which of those attempts they are looking at, as
 * `2/3`, because the region below the graph is showing exactly one attempt and a graph that
 * answered "3" while the reader read attempt 2 would be describing a different thing than the
 * page it sits on.
 *
 * The total is kept alongside the position rather than replaced by it: the reader who wanted to
 * know a step had run three times still wants to know it while reading one of the three. A step
 * that has run once has nothing to disambiguate and keeps its bare `1`.
 *
 * `selected` is the activity region's current block. Its attempt is null for admission, which is
 * not an attempt at anything and carries no badge at all.
 */
export function attemptBadges(
  runs: ReadonlyArray<{ step_name: string; attempt: number }>,
  selected?: { nodeId: string; attempt: number | null } | null,
): Record<string, string> {
  const counts = attemptCountsFromRuns(runs);
  const badges: Record<string, string> = {};
  for (const [step, count] of Object.entries(counts)) badges[step] = String(count);
  if (selected == null || selected.attempt == null) return badges;
  // The timeline and the ticket are two separate polls, so the region can be showing an attempt
  // whose `step_run` row has not arrived yet. Trust the attempt the reader is in over the count.
  const total = Math.max(counts[selected.nodeId] ?? 0, selected.attempt);
  if (total > 1) badges[selected.nodeId] = `${selected.attempt}/${total}`;
  return badges;
}

/**
 * The tone each step draws in, keyed by step name — the graph's half of the one status vocabulary.
 *
 * Two sources, in this order:
 *
 * 1. Every step's LATEST attempt, through `runTone`. This is the history mark: a step the ticket
 *    has already left still says how it ended, so a gate that was rejected and looped back is not
 *    drawn as the clean success it is not. Attempts, not rows — a step that failed and then passed
 *    on the retry ended well and takes no mark.
 * 2. The ticket's own condition, over the step it is stopped on. This is what the run status alone
 *    cannot say: whether the ticket is merely between attempts or actually parked. An interrupted
 *    run and an escalation both leave a finished run row, and only the ticket knows which of them
 *    is still waiting for a person.
 *
 * Steps whose tone is `active`, `neutral` or `success` are absent: those keep the graph's own
 * treatment (see `NodeTone`), so the returned map holds only the nodes that need saying something
 * about.
 */
export function nodeTones(
  runs: ReadonlyArray<{
    step_name: string;
    attempt: number;
    status: string;
    forced_by?: string | null;
    superseded_at?: string | null;
    conflict_send_back?: boolean;
  }>,
  ticket?:
    | (Pick<TicketDetail, "status" | "current_step"> & Partial<Pick<TicketDetail, "park_reason">>)
    | null,
): Record<string, NodeTone> {
  const latest = new Map<string, { attempt: number; shown: RunShown }>();
  for (const run of runs) {
    const prev = latest.get(run.step_name);
    if (!prev || run.attempt > prev.attempt) {
      const shown = runShown({ ...run, status: run.status as StepRunStatus });
      latest.set(run.step_name, { attempt: run.attempt, shown });
    }
  }
  const tones: Record<string, NodeTone> = {};
  for (const [step, { shown }] of latest) {
    const tone = runTone(shown);
    if (isNodeTone(tone)) tones[step] = tone;
  }
  if (ticket) {
    const condition = conditionOf({
      status: ticket.status,
      latestRunStatus: latestRunStatusAt(runs, ticket.current_step),
      parkReason: ticket.park_reason,
    });
    const tone = CONDITION_TONES[condition];
    if (isNodeTone(tone)) tones[ticket.current_step] = tone;
    // A ticket that has moved on erases the mark on the step it is standing on: `running` and
    // `retrying` are not states a node reports, and leaving the last attempt's tone there would
    // colour a step that is under way again by how its previous try ended.
    else delete tones[ticket.current_step];
  }
  return tones;
}
