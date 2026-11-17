import type { StepRun, TimelineEntry } from "../../api/types";
import { str } from "./details";

/** An idle run's session is still going: its agent is waiting on a person, and turns resume. */
export function isLive(run: StepRun): boolean {
  return run.status === "running" || run.status === "idle";
}

/**
 * The run's agent is at work now. Not an idle run, whose agent waits on a person, and not a check
 * step's `running`, which is the loop's own wait on observed state with no agent behind it.
 */
export function isWorking(run: StepRun): boolean {
  return run.kind === "agent" && run.status === "running" && !run.superseded_at;
}

/** The events that set an agent working: its spawn, a resume after a stop, and a reply that woke
 *  an idle session. */
const SET_WORKING = new Set(["agent_spawned", "agent_resumed", "agent_active"]);

/**
 * When the agent's current stretch of work began: the newest event that set it working, else the
 * run's start. Not the run's start alone, which would count the time a stopped or idle session
 * spent waiting on a person as work.
 */
export function workingSince(run: StepRun, entries: readonly TimelineEntry[]): string | null {
  let since: TimelineEntry | undefined;
  for (const entry of entries) {
    // By id, not position: the panel hands the rows newest first by default.
    if (SET_WORKING.has(entry.type) && (since === undefined || entry.id > since.id)) since = entry;
  }
  return since?.created_at ?? run.started_at;
}

/** The runs whose files the worker records: `worker/branches.py::ENDED`. An interrupted run has
 *  not ended, since its resume may still push. */
const ENDED = new Set<string>(["succeeded", "failed", "timeout", "cancelled"]);

/** The run will not push, or publish, anything more. */
export function hasEnded(run: StepRun): boolean {
  return ENDED.has(run.status);
}

/**
 * What an `agent_spawned` event says a run was started on.
 *
 * `runtimeSlug` is the registry row the run was spawned against, which a reader can open at
 * `/runtimes/<slug>`; null on an event that names none. `substrate` is the adapter kind
 * (`claude_cli`), which names no runtime row, so it is kept for `sessionReference` alone and never
 * printed as the runtime.
 */
export interface Spawn {
  agent: string | null;
  runtimeSlug: string | null;
  runtimeType: string | null;
  substrate: string | null;
}

/**
 * `runtime_run_id -> Spawn`, read off the `agent_spawned` entries.
 *
 * Not from `step_run.runtime_kind`: that is the kind the agent's MANIFEST declares, and a
 * deployment may run a manifest on a substrate it does not name. The event records what actually
 * ran, and the registry row it ran on, which the id on `step_run` loses when that row is deleted.
 */
export function spawnsFrom(entries: readonly TimelineEntry[]): Map<string, Spawn> {
  const byRunId = new Map<string, Spawn>();
  for (const entry of entries) {
    if (entry.type !== "agent_spawned") continue;
    const runId = str(entry.details.run_id);
    if (!runId) continue;
    byRunId.set(runId, {
      agent: str(entry.details.agent),
      runtimeSlug: str(entry.details.runtime_slug),
      runtimeType: str(entry.details.runtime_type),
      substrate: str(entry.details.runtime),
    });
  }
  return byRunId;
}

/** What a ticket has spent so far, across every attempt of every step. */
export interface ActivityTotals {
  /** How many runs went into these figures. Zero means there is nothing to show. */
  runs: number;
  inputTokens: number;
  outputTokens: number;
  /** Summed in ten-thousandths and handed back as a decimal string, like the API sends it. */
  costUsd: string;
  /** Whether any run carried a cost at all. False draws `—` rather than a `$0.00` that would
   *  call an unpriced run free. */
  priced: boolean;
  /** Whether any run's figure is a list-price estimate: one estimated part makes the sum one. */
  estimated: boolean;
  /** Wall time over every run. A run still going counts up to now. */
  seconds: number;
}

/**
 * Aggregate the step runs for the Activity header.
 *
 * Runs are summed rather than measured end to end from admission, because the ticket's elapsed
 * time is mostly the gaps: a ticket that waited overnight for a reviewer did not spend the night
 * working. Gate runs are included — waiting for a human IS where a ticket's time goes, and it is
 * the figure someone chasing a slow ticket is looking for.
 *
 * Cost is accumulated in integer ten-thousandths for the same reason `cost_usd` crosses the wire
 * as a string: adding a column of floats drifts, and the total is the number a reader trusts.
 */
export function totalsFrom(runs: readonly StepRun[], now: number = Date.now()): ActivityTotals {
  let tenThousandths = 0;
  let inputTokens = 0;
  let outputTokens = 0;
  let millis = 0;
  let priced = false;
  let estimated = false;

  for (const run of runs) {
    if (run.cost_usd !== null) {
      priced = true;
      if (run.cost_source === "estimated") estimated = true;
      const amount = Number(run.cost_usd);
      if (Number.isFinite(amount)) tenThousandths += Math.round(amount * 10_000);
    }
    inputTokens += run.input_tokens ?? 0;
    outputTokens += run.output_tokens ?? 0;
    if (run.started_at) {
      const end = run.ended_at ? new Date(run.ended_at).getTime() : now;
      millis += Math.max(0, end - new Date(run.started_at).getTime());
    }
  }

  return {
    runs: runs.length,
    inputTokens,
    outputTokens,
    costUsd: (tenThousandths / 10_000).toFixed(4),
    priced,
    estimated,
    seconds: millis / 1000,
  };
}
