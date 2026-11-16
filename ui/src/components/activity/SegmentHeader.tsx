import type { StepRun } from "../../api/types";
import { formatCost, formatRunTime, formatTokens } from "../../format";
import { runShown } from "../../status";
import {
  Button,
  Code,
  ConfirmPopup,
  HoverCard,
  Icon,
  Lozenge,
  Metric,
  StatusLozenge,
  VisuallyHidden,
} from "../ui";
import type { ActivitySegment, Spawn } from "./activity";
import { ForceComplete } from "./ForceComplete";
import type { ForceCompleteProps } from "./force";
import { type RedoOffer, RedoStep } from "./RedoStep";
import { RetryStep } from "./RetryStep";
import { RunRuntime } from "./RunRuntime";
import type { RetryOffer } from "./retry";
import { isLive } from "./runs";

/** What a block's run can open in the Output drawer, a button each: each opens its own section
 *  alone, the other a bar beside it. */
export interface OutputOpeners {
  /** The run's terminal, with its transcript behind a switch: an agent run that spawned. */
  terminal?: (run: StepRun) => void;
  /** The step's preview, which the pinned config declares, called Files when it declares files. */
  preview?: { label: "Preview" | "Files"; open: (run: StepRun) => void };
}

/**
 * Sticky, so the step you are reading stays named while its events scroll past. This carries what
 * the step-runs table used to: status and attempt in the row, and the model, duration, tokens and
 * cost behind a metrics icon (`RunFigures`), so the row keeps its actions on one line — plus
 * Terminal and Preview (or Files), the ways into the run's terminal, transcript and preview
 * (`OutputDrawer`), the unbounded content that has nowhere else to be reached from.
 *
 * A failed run's reason is deliberately NOT repeated here: the `agent_failed` / `action_failed`
 * row below reports the same `failure_reason`, with its own way into the full text.
 *
 * Interrupt confirms in a popup anchored to its own button, the way Exit does in the page header
 * (decision 50): it stops work that is running and costs the reader a restart to undo.
 */
export function SegmentHeader({
  segment,
  runtimeFor,
  output,
  onInterrupt,
  interrupting,
  retry,
  forceComplete,
  redo,
  starting,
  startingRuntime,
  hidden = 0,
}: {
  segment: ActivitySegment;
  runtimeFor: (run: StepRun) => Spawn | undefined;
  /** Absent for a run with no terminal or preview; no button is then drawn. */
  output?: OutputOpeners;
  onInterrupt?: (stepRunId: number) => void;
  interrupting?: boolean;
  /** This is the failed attempt a Retry would follow (`retryOffer`). */
  retry?: RetryOffer;
  /** This is the attempt Force complete would accept (`forceOffer`). */
  forceComplete?: ForceCompleteProps;
  /** This is the current step's latest attempt, and a redo would take its step. */
  redo?: RedoOffer;
  starting?: boolean;
  startingRuntime?: Spawn | null;
  /** How many of the block's rows the activity's Focused switch left out. */
  hidden?: number;
}) {
  const run = segment.run;

  return (
    <header className="sticky top-0 z-card flex flex-wrap items-center gap-x-3 gap-y-1 border-y border-default bg-surface px-4 py-2">
      <Code>{segment.step}</Code>
      {segment.attempt !== null && segment.attempt > 1 && (
        <Lozenge>Attempt {segment.attempt}</Lozenge>
      )}
      {run && <StatusLozenge status={runShown(run)} />}
      {hidden > 0 && (
        <span
          data-hidden-rows
          className="inline-flex items-center gap-1 text-body-sm text-subtle tabular-nums"
        >
          <Icon glyph="eye-off" size="small" />
          {hidden}
          <VisuallyHidden>{` routine ${hidden === 1 ? "event" : "events"} hidden`}</VisuallyHidden>
        </span>
      )}
      {run && runShown(run) === "forced" && (
        <span className="text-body-sm text-subtle">by {run.forced_by}</span>
      )}
      {!run && starting && <Lozenge>Starting</Lozenge>}
      {run?.decided_by && <span className="text-body-sm text-subtle">by {run.decided_by}</span>}
      {run ? (
        <RunRuntime spawn={runtimeFor(run)} model={null} />
      ) : (
        starting && startingRuntime && <RunRuntime spawn={startingRuntime} model={null} planned />
      )}

      <span className="ml-auto flex flex-wrap items-center gap-x-3 gap-y-1 text-subtle">
        {run && (
          <HoverCard label="Run metrics" icon="chart">
            <RunFigures run={run} />
          </HoverCard>
        )}
        {/* Only a running or idle AGENT step can be interrupted: a check step's RUNNING row is
            the loop's own wait on observed state — there is no session to stop, and the API
            refuses the request with a 409. It stops the run and keeps its workspace, so it can
            be resumed. */}
        {run &&
          (run.status === "running" || run.status === "idle") &&
          run.kind === "agent" &&
          onInterrupt && (
            <ConfirmPopup
              label="Interrupt"
              name="Interrupt step"
              title="Interrupt this step?"
              confirmLabel="Interrupt step"
              consequence="The agent stops and its workspace is kept. Resume continues the same session."
              triggerAppearance="warning"
              spacing="compact"
              // The run, not the ticket: a poll that ends this run under the reader closes a
              // confirmation nobody gave for a run that is no longer going.
              resetKey={`${run.id}-${run.status}`}
              isPending={interrupting}
              onConfirm={() => onInterrupt(run.id)}
            />
          )}
        {run && redo && (
          <RedoStep
            ticketId={redo.ticketId}
            projectId={redo.projectId}
            step={redo.step}
            label="Redo"
            resetKey={`${run.id}-${run.status}`}
          />
        )}
        {retry && <RetryStep {...retry} />}
        {forceComplete && <ForceComplete {...forceComplete} />}
        {run && output?.terminal && (
          <Button appearance="default" spacing="compact" onClick={() => output.terminal?.(run)}>
            Terminal
          </Button>
        )}
        {run && output?.preview && (
          <Button appearance="default" spacing="compact" onClick={() => output.preview?.open(run)}>
            {output.preview.label}
          </Button>
        )}
      </span>
    </header>
  );
}

/**
 * A run's model, duration, tokens and cost, in its metrics icon's card.
 *
 * An agent run that has not ended carries its session's figures so far (decision 56): counted off
 * the agent's own log and priced at list, until the end's figure replaces them. A run the worker
 * ends itself, a timeout or a failure, keeps the last of them. Why a cost is an estimate is said
 * in the card, not behind a second icon: the card holds text only (`HoverCard`).
 */
export function RunFigures({ run }: { run: StepRun }) {
  // Each row's term names its figure, so no figure carries a label of its own as well.
  const tokens =
    run.input_tokens !== null || run.output_tokens !== null
      ? formatTokens(run.input_tokens, run.output_tokens)
      : null;
  const soFar = run.kind === "agent" && (isLive(run) || run.status === "interrupted");
  const estimated = Boolean(run.cost_usd) && run.cost_source === "estimated";
  return (
    <dl className="grid grid-cols-[max-content_auto] items-center gap-x-4 gap-y-2 p-3">
      {run.model && (
        <div className="contents">
          <dt className="text-subtle">Model</dt>
          <dd>{run.model}</dd>
        </div>
      )}
      <div className="contents">
        <dt className="text-subtle">Duration</dt>
        <dd>
          <Metric>{formatRunTime(run.started_at, run.ended_at)}</Metric>
        </dd>
      </div>
      {tokens !== null && (
        <div className="contents">
          <dt className="text-subtle">{soFar ? "Tokens so far" : "Tokens"}</dt>
          <dd>
            <Metric>{tokens}</Metric>
          </dd>
        </div>
      )}
      {run.cost_usd && (
        <div className="contents">
          <dt className="text-subtle">{soFar ? "Cost so far" : "Cost"}</dt>
          <dd>
            <Metric>{formatCost(run.cost_usd, estimated)}</Metric>
          </dd>
          {estimated && (
            <dd className="col-span-2 max-w-64 text-subtle">
              {soFar
                ? "An estimate: the session is still running. Tokens are counted from the agent's log and priced at list rates. The final cost replaces this figure when the run ends."
                : "An estimate from the session's tokens at list rates. The model reports no price, or the run ended before it reported one, so this is the last figure counted while it ran. The provider's bill can differ."}
            </dd>
          )}
        </div>
      )}
    </dl>
  );
}
