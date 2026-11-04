import type { StepKind, StepRun, TicketDetail, TimelineEntry } from "../../api/types";
import {
  conditionOf,
  latestRunAt,
  ROLE_DOT,
  type RunShown,
  runShown,
  runTone,
  type TicketCondition,
  TONE_ROLE,
} from "../../status";
import { ENTRY_NODE_ID } from "../flow/processGraph";
import { str } from "./details";

export interface ActivitySegment {
  /** `step#attempt`, or `restart@<event id>` / `redo@<event id>` for a restart's or a redo's
   *  block, with `@g<generation>` after an earlier generation's `step#attempt` that a later one
   *  numbers again. Stable across refetches until that happens, and used as the scroll target. */
  key: string;
  /** The graph node this block belongs to — a step name, or the synthetic entry node. */
  nodeId: string;
  step: string;
  /** Null for admission, a restart and a redo, none of which is an attempt at anything. */
  attempt: number | null;
  kind: StepKind | "entry";
  /** The `step_run` row, when there is one. Admission has none. */
  run: StepRun | null;
  entries: TimelineEntry[];
  /** `runShown` of the run: a forced run is `forced`. */
  status: RunShown | "info";
  /** The run's generation; for a block with no run, the one in force when its first event was
   *  written. A restart's block is the generation it opens. */
  generation: number;
}

/**
 * Group the timeline into per-attempt blocks, in log order.
 *
 * An event naming two attempts — `attempt`, the one that just ended, and `next_attempt`, the one
 * it is booking — is filed under the one it books, so it opens the next block rather than closing
 * the previous one. That is the right reading: the block for attempt 2 starts with the decision to
 * make attempt 2, and while the wait runs that block is the only thing on the page that says
 * anything is still coming. Two events book: `retry_scheduled`, the failure ladder's own booking,
 * and `unblocked`, the human gesture that grants one past it.
 *
 * A run with no events of its own still gets a block. The ticket and the timeline are two separate
 * polls (`useTicket` / `useTimeline`), so a run that has just started is routinely visible on one
 * before the other — and a step run silently missing from the only view of the ticket is worse
 * than an empty block. Those go last: a run without events is, in practice, the newest thing.
 *
 * Each generation keeps its own blocks. Attempt numbers count on across a restart, but from the
 * rows, so an attempt a generation booked or entered and never ran is numbered again by the next
 * one, and one block would hold both sides of the restart. An event about a run files under that
 * run's generation, since an earlier run can still report after a restart.
 *
 * A redo is the same kind of boundary for the steps it discards: each step counts the redos
 * that discarded it, and a block belongs to one of those epochs. The redo's fresh attempt can
 * carry the number an earlier booking already used, as a booked retry that never ran, so
 * without the epoch the fresh attempt would join that booking's block, above the redo. The
 * request a redo answers files under the redo's own block, with its note.
 */
export function segmentsFrom(
  entries: readonly TimelineEntry[],
  runs: readonly StepRun[],
): ActivitySegment[] {
  const runBy = new Map<string, StepRun>();
  for (const run of runs) runBy.set(`${run.step_name}#${run.attempt}`, run);

  // The `step_redone` that answers each redo request, so the request joins the redo's block.
  const redoneFor = new Map<unknown, number>();
  for (const entry of entries) {
    if (entry.type === "step_redone") redoneFor.set(entry.details.request_id, entry.id);
  }

  // By generation, epoch and key; `distinctKeys` settles the keys once every block is known.
  const order: { group: string; epoch: number }[] = [];
  const byGroup = new Map<string, ActivitySegment>();
  // Per `generation:key`, the epoch of the block of it that opened last.
  const epochsAt = new Map<string, number>();
  const redos = new Map<string, number>();
  let current = 1;
  let latest: ActivitySegment | undefined;

  for (const entry of entries) {
    // A webhook names no step: it is a wake-up (design.md §2.1), so it joins the block it woke
    // rather than one "unknown" block that gathers every delivery.
    if (entry.type === "webhook_received" && latest) {
      latest.entries.push(entry);
      continue;
    }
    const isAdmission = entry.type === "ticket_admitted";
    // A restart is a way in, as admission is, so it sits on the entry node: a block per restart,
    // keyed by its event, because two restarts onto one entry step are two boundaries. The worker
    // appends it ahead of the `step_entered` that opens the new generation.
    const isRestart = entry.type === "ticket_restarted";
    if (isRestart) {
      const opens = entry.details.generation;
      current = typeof opens === "number" ? opens : current + 1;
    }
    // A redo is a boundary too, on the step it starts again; the worker appends it ahead of that
    // step's `step_entered`.
    const redoId =
      entry.type === "step_redone"
        ? entry.id
        : entry.type === "redo_requested"
          ? redoneFor.get(entry.id)
          : undefined;
    const isRedo = redoId !== undefined;
    if (entry.type === "step_redone") {
      const discarded = Array.isArray(entry.details.discarded_steps)
        ? entry.details.discarded_steps
        : [entry.details.target_step];
      for (const name of discarded) {
        if (typeof name === "string") redos.set(name, (redos.get(name) ?? 0) + 1);
      }
    }
    const entering = isAdmission || isRestart;
    const step = entering
      ? ENTRY_NODE_ID
      : ((isRedo ? str(entry.details.target_step) : str(entry.details.step)) ?? "unknown");
    // Read off the key, not from a per-type list — `linkFor`'s reason, that the same key means
    // the same thing wherever it appears. The list is what went stale when `unblocked` grew a
    // `next_attempt` of its own and kept being filed under the attempt that had just failed.
    const attemptRaw =
      typeof entry.details.next_attempt === "number"
        ? entry.details.next_attempt
        : entry.details.attempt;
    const attempt = entering || isRedo || typeof attemptRaw !== "number" ? null : attemptRaw;
    const key = isRestart
      ? `restart@${entry.id}`
      : isRedo
        ? `redo@${redoId}`
        : attempt === null
          ? step
          : `${step}#${attempt}`;
    const known = attempt === null ? undefined : runBy.get(key);
    const generation = known ? Math.min(current, known.generation) : current;
    // A late report about a run a redo set aside, or about an earlier generation's run, joins
    // that run's block rather than opening one in the step's newer epoch.
    const opened = epochsAt.get(`${generation}:${key}`);
    const epoch = isRedo
      ? 0
      : opened !== undefined && known && (known.superseded_at || known.generation < current)
        ? opened
        : (redos.get(step) ?? 0);
    const group = `${generation}:${epoch}:${key}`;

    let segment = byGroup.get(group);
    if (!segment) {
      const run = known?.generation === generation ? known : null;
      segment = {
        key,
        nodeId: step,
        step: isAdmission ? "admission" : isRestart ? "restart" : isRedo ? "redo" : step,
        attempt,
        kind:
          entering || isRedo
            ? "entry"
            : (run?.kind ?? (str(entry.details.kind) as StepKind) ?? "agent"),
        run,
        entries: [],
        status: run ? runShown(run) : "info",
        generation,
      };
      byGroup.set(group, segment);
      epochsAt.set(`${generation}:${key}`, epoch);
      order.push({ group, epoch });
    }
    const last = segment.entries.at(-1);
    if (last && typingBurst(last, entry)) {
      segment.entries[segment.entries.length - 1] = {
        ...entry,
        id: last.id,
        details: {
          ...entry.details,
          bytes: Number(last.details.bytes) + Number(entry.details.bytes),
        },
      };
    } else {
      segment.entries.push(entry);
    }
    latest = segment;
  }

  const orphans = [...runs]
    .filter((run) => !epochsAt.has(`${run.generation}:${run.step_name}#${run.attempt}`))
    .sort((a, b) => a.created_at.localeCompare(b.created_at))
    .map(
      (run): ActivitySegment => ({
        key: `${run.step_name}#${run.attempt}`,
        nodeId: run.step_name,
        step: run.step_name,
        attempt: run.attempt,
        kind: run.kind,
        run,
        entries: [],
        status: runShown(run),
        generation: run.generation,
      }),
    );

  // A key in two epochs is a booking or entry the redo overtook, then the attempt that ran: the
  // run is the newest epoch's, and the earlier block takes `@r<epoch>`.
  const blocks = order.map(({ group, epoch }) => {
    const segment = byGroup.get(group) as ActivitySegment;
    const newest = epochsAt.get(`${segment.generation}:${segment.key}`) ?? epoch;
    return epoch === newest
      ? segment
      : { ...segment, key: `${segment.key}@r${epoch}`, run: null, status: "info" as const };
  });
  return distinctKeys([...blocks, ...orphans]);
}

/** The latest generation to use a key keeps it, because `heldAt` and the page's other
 *  current-attempt reads build it from the current runs; an earlier one takes `@g<generation>`. */
function distinctKeys(segments: ActivitySegment[]): ActivitySegment[] {
  const latest = new Map<string, number>();
  for (const { key, generation } of segments) {
    latest.set(key, Math.max(latest.get(key) ?? generation, generation));
  }
  return segments.map((segment) =>
    segment.generation < (latest.get(segment.key) ?? segment.generation)
      ? { ...segment, key: `${segment.key}@g${segment.generation}` }
      : segment,
  );
}

/** The gateway writes a writer's typing as a burst every few seconds: one row per stretch of it,
 *  kept under the first burst's id and dated by the last. */
function typingBurst(last: TimelineEntry, next: TimelineEntry): boolean {
  return (
    last.type === "terminal_input" &&
    next.type === "terminal_input" &&
    last.details.viewer !== undefined &&
    last.details.viewer === next.details.viewer &&
    typeof last.details.bytes === "number" &&
    typeof next.details.bytes === "number"
  );
}

/** Which end of the log the reader wants first. */
export type ActivityOrder = "newest" | "oldest";

/**
 * Re-read the same blocks from the other end of the log.
 *
 * Reversal, not a sort by timestamp: `segmentsFrom` has already put the log in the only order
 * that is true — the event ids' total order, with the orphan runs (a run whose events have not
 * arrived on this poll yet) pinned to the newest end. Sorting by `created_at` would break the
 * ties inside a single tick, where several events share a timestamp to the second, and would
 * strand the orphans, which have no event timestamp at all.
 *
 * A block's own events turn around with it. Reversing only the blocks would leave each one
 * reading oldest-first inside a newest-first page, which is the one arrangement that tells the
 * reader nothing about where to look.
 */
export function sortSegments(
  segments: readonly ActivitySegment[],
  order: ActivityOrder,
): ActivitySegment[] {
  if (order === "oldest") return [...segments];
  return [...segments]
    .reverse()
    .map((segment) => ({ ...segment, entries: [...segment.entries].reverse() }));
}

/**
 * Which attempt block a click on a graph node should land on.
 *
 * The graph draws one node per step and knows nothing about attempts, so this click is the only
 * control there is for reaching attempt 2 of 3. Two behaviours in one:
 *
 * - A node the reader is not already in opens at its MOST RECENT attempt. A step that ran three
 *   times is being clicked because of what happened last, not because of how it started.
 * - Clicking the node the reader is already in steps back to the attempt before, and wraps round
 *   to the most recent again. Repeated clicks therefore walk the step's history newest to oldest,
 *   which is the order the retries make sense in — each one is a reaction to the one before it.
 *
 * The step's own attempt numbers order the cycle, not the position of the blocks in `segments`:
 * "most recent" is a fact about the ticket, and the reader flipping the region to newest-first
 * must not turn the first click into the oldest attempt. One attempt means every click returns
 * the same key, which is the correct nothing.
 */
export function attemptKeyForNode(
  segments: readonly ActivitySegment[],
  nodeId: string,
  activeKey: string | null,
): string | null {
  const attempts = segments
    .filter((segment) => segment.nodeId === nodeId)
    // Attempts are numbered from 1 and never reused, so this is the step's history. Admission has
    // no attempt and no siblings; a stable sort leaves any other unnumbered block where it lay.
    .sort((a, b) => (a.attempt ?? 0) - (b.attempt ?? 0));
  if (attempts.length === 0) return null;
  const index = attempts.findIndex((segment) => segment.key === activeKey);
  const next = index < 0 ? attempts.length - 1 : (index - 1 + attempts.length) % attempts.length;
  return attempts[next]?.key ?? null;
}

/** The ATTEMPT a ticket has stopped at, and the condition that stopped it. */
export interface Held {
  /** `segmentsFrom`'s `step#attempt` key, so a block can ask whether it is the one. */
  key: string;
  /** The whole ticket's condition — the same value the header pill and the graph node draw. */
  condition: TicketCondition;
}

/**
 * Where the ticket is stopped, if it is stopped: waiting on a person, or blocked.
 *
 * Keyed to one attempt, not to the step. A gate that was rejected and reopened has three blocks
 * on screen and only the newest asks anything of anyone; the earlier ones are decided history,
 * and marking them all would tell the reader to do the same thing three times.
 *
 * The condition travels with the key rather than a bare "is this a failure" flag, so the link and
 * the node it points at cannot disagree about either the beat or the colour: an interrupted run
 * and the gate above it are both amber and both blink plainly, while only a real escalation gets
 * the error beat.
 */
export function heldAt(
  ticket: Pick<TicketDetail, "status" | "current_step"> &
    Partial<Pick<TicketDetail, "park_reason">>,
  runs: readonly StepRun[],
): Held | null {
  if (ticket.status !== "waiting_gate" && ticket.status !== "blocked") return null;
  const latest = latestRunAt(runs, ticket.current_step);
  // The ticket and the timeline are separate polls, so the step can be current before its run row
  // has arrived. Nothing is on screen for it yet, so there is nothing to mark.
  if (!latest) return null;
  return {
    key: `${ticket.current_step}#${latest.attempt}`,
    condition: conditionOf({
      status: ticket.status,
      latestRunStatus: latest.status,
      parkReason: ticket.park_reason,
    }),
  };
}

/**
 * The rail's dot, resolved through the same tone table `StatusLozenge` uses.
 *
 * A switch of its own is what let this drift: it listed six of the nine run statuses and answered
 * grey for the rest, so an `interrupted` attempt wore an amber tag above a slate dot. One table
 * cannot do that.
 */
export function statusDot(status: ActivitySegment["status"]): string {
  // `info` is the admission block, which is not an attempt at anything and has no status to show.
  return ROLE_DOT[TONE_ROLE[status === "info" ? "neutral" : runTone(status)]];
}
