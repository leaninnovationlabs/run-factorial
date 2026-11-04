/**
 * The preview view's decisions, apart from its drawing: which steps offer one (from the PINNED
 * config), and which run the waiting gate can send back for changes right now.
 *
 * `gateReview` mirrors the API's own check (`api/routes/runs.py::_reviewing_gate_run`), so
 * Request changes is offered exactly when the request would be accepted. The API still decides:
 * this only spares a reviewer a form that could never go anywhere.
 */

import type { StepRun, TicketDetail } from "../../api/types";
import { type ProcessConfigLike, rerunStepOf } from "../flow";

/** The step's enabled `preview:` block in its pinned config, or null. A block pinned with a
 *  retired `page` and nothing else is none: it never waited for an outside URL
 *  (`Preview.only_a_retired_page`). */
function previewOf(
  config: ProcessConfigLike | null,
  stepName: string,
): Record<string, unknown> | null {
  const step = config?.steps.find((candidate) => candidate.name === stepName);
  const preview = step?.preview;
  if (typeof preview !== "object" || preview === null) return null;
  const block = preview as Record<string, unknown>;
  if (block.enabled === false) return null;
  const has = (key: string) => Array.isArray(block[key]) && (block[key] as unknown[]).length > 0;
  const onlyAPage = (block.page ?? null) !== null && !has("files") && !has("services");
  return onlyAPage ? null : block;
}

/** Whether the step's pinned config declares an enabled `preview:` block. */
export function hasPreview(config: ProcessConfigLike | null, stepName: string): boolean {
  return previewOf(config, stepName) !== null;
}

/** Whether that block declares any `files`, which its section is named for. A block with none
 *  shows only a live preview: the retired `page` a pinned snapshot may still carry shows nothing. */
export function declaresFiles(config: ProcessConfigLike | null, stepName: string): boolean {
  const files = previewOf(config, stepName)?.files;
  return Array.isArray(files) && files.length > 0;
}

export interface GateReview {
  /** The gate the ticket waits at. */
  gate: string;
  /** What it sends back: the latest attempt of its `onReject.rerunStep`. */
  run: StepRun;
}

function latestOf(runs: StepRun[], name: string): StepRun | null {
  return runs.reduce<StepRun | null>(
    (best, candidate) =>
      candidate.step_name === name && (best === null || candidate.id > best.id) ? candidate : best,
    null,
  );
}

/**
 * The run a dashboard change request at the ticket's waiting gate sends back, or null: only
 * while the ticket waits at a human gate whose `onReject` reruns an agent step, and only that
 * step's latest attempt, once it succeeded and before the gate's waiting run opened.
 */
export function gateReview(
  config: ProcessConfigLike | null,
  ticket: Pick<TicketDetail, "current_step" | "status">,
  runs: StepRun[],
): GateReview | null {
  if (!config || ticket.status !== "waiting_gate" || !ticket.current_step) return null;
  const gate = config.steps.find((step) => step.name === ticket.current_step);
  if (gate?.kind !== "human_gate") return null;
  const rerun = rerunStepOf(gate);
  const target = config.steps.find((step) => step.name === rerun);
  if (target?.kind !== "agent") return null;
  const run = latestOf(runs, target.name);
  const gateRun = latestOf(runs, gate.name);
  if (run?.status !== "succeeded") return null;
  if (gateRun?.status !== "waiting" || gateRun.id < run.id) return null;
  return { gate: gate.name, run };
}

/** The gate `run` can be sent back through now, or null: `gateReview`, for this run alone. */
export function reviewTarget(review: GateReview | null, run: StepRun): string | null {
  return review !== null && review.run.id === run.id ? review.gate : null;
}
