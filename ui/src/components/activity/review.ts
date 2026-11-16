/**
 * Approve's decisions, apart from its drawing: which steps await a person's approval (from the
 * PINNED config, so a step that does not never reads the forge), which runs are worth reading the
 * review of, and what that review offers. The API decides: `offered` says whether Approve applies
 * to the run at all, and `refusal` is its own words for why a press now would be refused.
 */

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { keys, useReview } from "../../api/queries";
import type { ParkReason, Review, StepRun } from "../../api/types";
import { conditionCalls, type ProcessConfigLike } from "../flow";

/** Whether the step's pinned postconditions call `request_approval()`. Config refuses the
 *  predicate anywhere else, so the postconditions are the one place to look. */
export function stepAwaitsReview(config: ProcessConfigLike | null, stepName: string): boolean {
  const step = config?.steps.find((candidate) => candidate.name === stepName);
  if (step?.kind !== "agent") return false;
  return conditionCalls(step.postconditions).some((call) => /^request_approval\s*\(/.test(call));
}

/** A run that may wait at its prompt for a review: live and idle, or parked. */
function atPrompt(run: Pick<StepRun, "status">): boolean {
  return run.status === "idle" || run.status === "interrupted";
}

/** The parks the loop takes, the only ones that can leave a session at its prompt: a person's
 *  stop or an exited session never waits for a review (`events.read_models.parked_at_prompt`). */
const LOOP_PARKS: ReadonlySet<ParkReason> = new Set(["idle", "ceiling", "rollover"]);

/** Whether a run's review is worth reading: a step that awaits review, a run at its prompt, and,
 *  when the caller knows the run's park, not a person's stop nor an exited session. */
export function readsReview(
  run: Pick<StepRun, "status"> | null,
  awaitsReview: boolean,
  parkReason?: ParkReason | null,
): boolean {
  if (run === null || !awaitsReview || !atPrompt(run)) return false;
  if (run.status === "idle" || parkReason === undefined) return true;
  return parkReason !== null && LOOP_PARKS.has(parkReason);
}

export interface ReviewOffer {
  /** The API's review, while the run is one Approve may apply to. */
  review: Review | undefined;
  error: unknown;
  /** Draw Approve: the API offers it for this run, or its read failed and says so. */
  shown: boolean;
  /** Offered, a press would be accepted, and this run pushed the commit it would approve: the
   *  stop asks for a review, not an answer. */
  ready: boolean;
}

/** The one reading of a run's review, by `readsReview`. Only the ticket page `poll`s: everything
 *  below it shares the cache, and the poll keeps the run's Files in step with it
 *  (`useFilesFollowReview`). */
export function useReviewOffer(
  run: Pick<StepRun, "id" | "status"> | null,
  awaitsReview: boolean,
  { parkReason, poll = false }: { parkReason?: ParkReason | null; poll?: boolean } = {},
): ReviewOffer {
  const wanted = readsReview(run, awaitsReview, parkReason);
  const read = useReview(run?.id ?? 0, wanted, poll);
  // A cached review of a run that has moved on offers nothing.
  const review = wanted ? read.data : undefined;
  const error = wanted ? read.error : null;
  const offered = Boolean(review?.awaits_review && review.offered);
  useFilesFollowReview(poll ? (run?.id ?? 0) : 0, review);
  return {
    review,
    error,
    shown: offered || (review === undefined && error !== null),
    ready: Boolean(offered && review?.pushed && review.head_sha && review.refusal === null),
  };
}

/** Reads the run's Files again when its review shows a new head, or a refusal clears. A parked
 *  run's Files are otherwise read every 10 minutes, and a `files_stale` refusal clears once the
 *  worker records the new head: Approve would then take a commit whose files the open pane never
 *  showed. Any cleared refusal counts, since the review carries a refusal's words, not its code. */
function useFilesFollowReview(stepRunId: number, review: Review | undefined): void {
  const qc = useQueryClient();
  const seen = useRef<{ stepRunId: number; head: string | null; refused: boolean } | null>(null);
  useEffect(() => {
    if (stepRunId === 0 || review === undefined) return;
    const last = seen.current?.stepRunId === stepRunId ? seen.current : null;
    const head = review.head_sha ?? last?.head ?? null;
    const refused = review.refusal !== null;
    seen.current = { stepRunId, head, refused };
    if (last === null) return;
    if ((last.head !== null && head !== last.head) || (last.refused && !refused)) {
      void qc.invalidateQueries({ queryKey: keys.runOutputs(stepRunId) });
    }
  }, [qc, stepRunId, review]);
}
