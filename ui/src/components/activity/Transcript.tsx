/**
 * What an agent actually did, as a console log.
 *
 * It fetches on open rather than with the page: a transcript is large, most runs are never opened,
 * and a finished run's log never changes again — so only a running or idle one keeps polling.
 *
 * The turns are drawn as one continuous stream of monospaced lines rather than as a stack of
 * cards. A transcript IS a log: it is read top to bottom, its lines are short, and what a reader
 * looks for is the shape of the run — where the text stops and the tools start, which call went
 * red. Boxing each turn spent a border, a radius and two paddings per turn on saying "a turn ends
 * here", which the gutter already says, and it left the tool calls indented inside a card instead
 * of aligned in a column the eye can run down.
 *
 * So the layout is a console's: a fixed gutter carrying the turn ordinal on the turn's first line
 * and nothing after it, a one-glyph status column, and the line itself. Tool names sit in a column
 * of their own so that every target starts on the same vertical, whatever the call was.
 */

import {
  Fragment,
  memo,
  type ReactNode,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { ApiError } from "../../api/client";
import { useRunActivity } from "../../api/queries";
import type { ActivityTurn, StepRun } from "../../api/types";
import { Button, cx, EmptyState, ErrorMessage, Icon, SectionMessage, Spinner } from "../ui";
import { atFoot } from "./follow";
import { hasEnded, isLive } from "./runs";

/**
 * Where this run's session lives, phrased for the substrate that ran it.
 *
 * The substrate comes from `agent_spawned`, never `step_run.runtime_kind` — see
 * `runs.ts`'s `spawnsFrom`.
 *
 * The root of a local run is the deployment's `local_root`, which the UI does not know; the run
 * id under the placeholder is enough to find the file, and the file is the transcript of record — the
 * view above is derived from it at read time and nothing is copied to Postgres (decision 34).
 */
export function sessionReference(runtime: string | undefined, runId: string): string | null {
  switch (runtime) {
    case "claude_cli":
      return `<local_root>/runs/${runId}/`;
    case "codex_cli":
      return `<local_root>/runs/${runId}/stdout.jsonl`;
    case "claude_e2b":
    case "codex_e2b":
      // The worker's mirror of the sandbox's stream; `state.json` beside it names the sandbox.
      return `<e2b state_root>/runs/${runId}/stdout.jsonl`;
    default:
      // An unknown or unrecorded substrate gets the id and no claim about where to find it.
      return null;
  }
}

/**
 * A purged run is not a failure to report in red. The step row is kept forever because it is the
 * audit trail; the substrate's copy of the output is not, and is cleaned up with the worktree.
 * Saying so plainly beats an error box that reads like something broke.
 */
function purgedMessage(error: unknown): string | null {
  return error instanceof ApiError && error.status === 410
    ? "This run's transcript was deleted after it finished. Its activity and costs are still on the ticket."
    : null;
}

/** The whole log as plain text, for the copy button: what a reader pastes into an issue. */
export function transcriptText(turns: ActivityTurn[], milestones: string[]): string {
  const lines = milestones.length > 0 ? [`# ${milestones.join(", ")}`] : [];
  for (const turn of turns) {
    for (const text of turn.text) lines.push(`${turn.index} > ${text}`);
    for (const call of turn.tools) {
      const mark = call.ok === false ? "[failed]" : call.ok ? "[ok]" : "[running]";
      lines.push(`${turn.index} ${mark} ${[call.tool, call.target].filter(Boolean).join(" ")}`);
      if (call.question) {
        lines.push(`${turn.index} Q: ${call.question} A: ${call.answer ?? "(no answer)"}`);
      }
    }
  }
  return lines.join("\n");
}

/**
 * The log itself, without the drawer around it (`OutputDrawer`). It shares the drawer's query by
 * key, so the transcript and the drawer's header never fetch the same run twice.
 */
export function TranscriptBody({ run }: { run: StepRun }) {
  const live = isLive(run);
  const activity = useRunActivity(run.id, Boolean(run.runtime_run_id), live);
  const purged = purgedMessage(activity.error);
  const data = activity.data;

  return (
    <div className="space-y-3 p-4">
      {purged ? (
        <SectionMessage appearance="information" title="Transcript deleted">
          {purged}
        </SectionMessage>
      ) : activity.error && !data ? (
        <ErrorMessage
          error={activity.error}
          title="Couldn't load this run's output"
          onRetry={() => activity.refetch()}
        />
      ) : activity.isPending ? (
        <Spinner label="Loading the transcript" />
      ) : !data?.turns.length ? (
        <EmptyState
          headingLevel={3}
          header={hasEnded(run) ? "This run recorded no output" : "Nothing captured yet"}
        />
      ) : (
        <Console turns={data.turns} milestones={data.milestones} live={live} />
      )}
    </div>
  );
}

/**
 * The log surface: one recessed block, no internal rules, nothing per turn but its ordinal.
 *
 * Every line wraps, at a space where it can and inside a long path where it must, so the log
 * never scrolls sideways.
 *
 * It scrolls inside itself so that a live run can be followed: the console sits at the foot of a
 * running log and stays there as turns arrive, and scrolling up leaves — see `follow.ts`. A
 * finished run opens at the top instead, because nothing is arriving and the top is where reading
 * starts.
 */
function Console({
  turns,
  milestones,
  live,
}: {
  turns: ActivityTurn[];
  milestones: string[];
  live: boolean;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [following, setFollowing] = useState(live);

  useEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    // Passive, like the activity region's probe: this only reads the scroll position.
    const update = () => setFollowing(atFoot(element));
    element.addEventListener("scroll", update, { passive: true });
    return () => element.removeEventListener("scroll", update);
  }, []);

  // Before paint, so an arriving turn is never briefly visible below the fold, and so that
  // pressing the button below lands in the same frame it was pressed. `turns` is the trigger and
  // not a value the body reads: every poll of a live run may have grown the last turn as well as
  // added one, and both change the height this has to chase.
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-running when the log grows is the intent
  useLayoutEffect(() => {
    const element = scrollRef.current;
    if (element && following) element.scrollTop = element.scrollHeight;
  }, [following, turns]);

  return (
    <div className="relative">
      <div
        ref={scrollRef}
        role="log"
        aria-label="Transcript"
        // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrolling log must be reachable by keyboard
        tabIndex={0}
        className="focus-inset relative max-h-[calc(100dvh-14rem)] overflow-auto overscroll-contain rounded-md border border-default bg-surface-sunken py-1.5 font-mono text-body-sm leading-relaxed"
      >
        {milestones.length > 0 && (
          <Line gutter="" mark="#" tone="text-subtlest">
            <span className="text-subtle">{milestones.join(" · ")}</span>
          </Line>
        )}
        {turns.map((turn) => (
          <TurnLines key={turn.index} turn={turn} />
        ))}
        {/* A run still writing gets a prompt at the foot of its own output, which is the shortest
            way to say "this is the end of the log so far" rather than "this is the end". Three
            beats, then still: a cursor that blinks for ever is a distraction, not news. */}
        {live && (
          <Line gutter="" mark="›" tone="text-subtlest">
            <span className="animate-pulse text-subtle motion-reduce:animate-none [animation-iteration-count:3]">
              ▍
            </span>
            <span className="sr-only">Still running</span>
          </Line>
        )}
      </div>
      {/* Only while the log is still being written, and only once the reader has left its foot:
          on a finished run there is no "latest" to be behind. */}
      {live && !following && (
        <div className="absolute right-3 bottom-3">
          <Button
            appearance="primary"
            spacing="compact"
            iconAfter="arrow-down"
            onClick={() => setFollowing(true)}
          >
            Jump to latest
          </Button>
        </div>
      )}
    </div>
  );
}

/**
 * One row of the console.
 *
 * The gutter is the same width on every line whether or not it carries a number, so the marks
 * below it form a column instead of stepping in and out as the turns change ordinal width.
 */
function Line({
  gutter,
  mark,
  tone,
  children,
}: {
  gutter: string;
  mark: ReactNode;
  /** The mark's colour. The line's own text sets its own. */
  tone: string;
  children: ReactNode;
}) {
  return (
    <div className="flex items-baseline gap-2 px-3 hover:bg-neutral-subtle-hovered">
      <span className="w-5 shrink-0 text-right text-subtlest tabular-nums select-none">
        {gutter}
      </span>
      <span className={cx("inline-flex w-3 shrink-0 justify-center self-center select-none", tone)}>
        {mark}
      </span>
      <span className="min-w-0 flex-1">{children}</span>
    </div>
  );
}

/** A tool call's outcome as a mark with a name, so a reader who cannot see the colour, or the
 *  shape, is told it too. */
function CallMark({ ok }: { ok: boolean | null | undefined }) {
  if (ok === false) return <Icon glyph="close" size="small" label="Failed" />;
  if (ok) return <Icon glyph="check" size="small" label="Succeeded" />;
  return (
    <span
      role="img"
      aria-label="Still running"
      className="size-1.5 animate-pulse rounded-full bg-chart-neutral motion-reduce:animate-none [animation-iteration-count:3]"
    />
  );
}

/** Every line one turn contributes, in the order it happened: what was said, then what was run. */
const TurnLines = memo(function TurnLines({ turn }: { turn: ActivityTurn }) {
  const [showThinking, setShowThinking] = useState(false);
  // The ordinal is printed once, against whichever line the turn happens to start with, and the
  // rest of the turn runs under a blank gutter — so a reader sees where a turn began without a
  // rule drawn across the log. A turn that said nothing starts with its first tool call.
  const ordinal = String(turn.index);
  const spoke = turn.text.length > 0;
  const acted = turn.tools.length > 0;

  return (
    <>
      {turn.text.map((text, i) => (
        <Line key={text.slice(0, 40)} gutter={i === 0 ? ordinal : ""} mark="›" tone="text-subtlest">
          <span className="whitespace-pre-wrap text-default">{text}</span>
        </Line>
      ))}

      {/* A tool call carries no id, and a transcript is append-only: position IS identity. */}
      {turn.tools.map((call, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: append-only transcript, index is stable
        <Fragment key={`${call.tool}-${i}`}>
          <Line
            gutter={!spoke && i === 0 ? ordinal : ""}
            mark={<CallMark ok={call.ok} />}
            tone={call.ok === false ? "text-icon-danger" : call.ok ? "text-icon-success" : ""}
          >
            <span className="flex flex-wrap items-baseline gap-2 [overflow-wrap:anywhere]">
              {/* A floor rather than a fixed width, so every target starts on the same vertical
                  while a name longer than the column — an MCP tool carries its server's prefix —
                  pushes its own target right instead of running underneath it. */}
              <span className="min-w-16 shrink-0 text-subtle">{call.tool}</span>
              <span className="text-subtle">{call.target}</span>
            </span>
          </Line>
          {call.question && (
            <Line gutter="" mark="?" tone="text-subtlest">
              <span className="whitespace-pre-wrap text-default">
                {call.question} <span className="text-subtlest">Answer:</span>{" "}
                {call.answer ?? (
                  <span className="text-subtle italic">
                    {call.ok === null ? "No answer yet" : "No answer"}
                  </span>
                )}
              </span>
            </Line>
          )}
        </Fragment>
      ))}

      {turn.thinking.length > 0 && (
        <>
          <Line gutter={spoke || acted ? "" : ordinal} mark="~" tone="text-subtlest">
            <Button
              spacing="compact"
              iconBefore={showThinking ? "chevron-down" : "chevron-right"}
              aria-expanded={showThinking}
              onClick={() => setShowThinking((v) => !v)}
            >
              Reasoning ({turn.thinking.length})
            </Button>
          </Line>
          {showThinking &&
            turn.thinking.map((text) => (
              <Line key={text.slice(0, 40)} gutter="" mark="" tone="">
                <span className="whitespace-pre-wrap text-subtle italic">{text}</span>
              </Line>
            ))}
        </>
      )}
    </>
  );
});
