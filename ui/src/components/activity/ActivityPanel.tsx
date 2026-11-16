/**
 * The ticket's activity: every step run and every event, in one region that scrolls inside itself.
 *
 * The region has a fixed height, so nothing a reader does here changes the length of the page.
 * That is the whole design constraint. The previous ticket page expanded transcripts and failure
 * reasons in place, which injected table rows, resized the page, and moved everything below the
 * thing being read — so every interaction cost the reader their place.
 *
 * Events sit under the attempt they belong to, so "which events belong to which step" is answered
 * by position rather than by a filter. The rail down the left says which attempt the viewport is
 * in and doubles as the jump control.
 *
 * Selection is two-way with the process graph above it: scrolling moves the graph's selected node,
 * and picking a node scrolls this region. `reportedRef` is what stops those two from driving each
 * other in a loop — see the comment on it.
 *
 * Focused leaves out the routine rows (`isRoutine`), by the same judgment that draws them dimmer.
 * A block keeps its header whatever it hides, and the header counts the rows it hid beside the
 * run's status; the rail counts the rows it shows.
 */

import {
  type ReactNode,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { StepRun, TimelineEntry } from "../../api/types";
import type { ForgeHost } from "../../forge";
import { Button, Code, cx, EmptyState, JumpRail, statusFacts, useScrollSpy } from "../ui";
import {
  type ActivityOrder,
  type ActivitySegment,
  currentRows,
  factsFor,
  type Held,
  isRoutine,
  type LongContent,
  type Spawn,
  statusDot,
} from "./activity";
import { EventRow } from "./EventRow";
import type { ForceCompleteProps } from "./force";
import type { RedoOffer } from "./RedoStep";
import { cancelledRetries, type RetryOffer } from "./retry";
import { type OutputOpeners, SegmentHeader } from "./SegmentHeader";

/** Distance below the region's top edge at which a block counts as "the one you are reading". */
const PROBE_OFFSET = 12;

/** What a step entered but not started waits for: its conditions in words, and the calls the
 *  pinned config wrote, shown only where there are no words. */
export interface Waiting {
  needs: readonly string[];
  calls: readonly string[];
}

export interface ActivityPanelProps {
  /** Already in reading order — `sortSegments` decides which end of the log comes first. */
  segments: ActivitySegment[];
  /** Which end that is. Only used to return the reader to the top when it flips. */
  order: ActivityOrder;
  /** `owner/name`, for the forge links in the rows. Absent until the project query lands. */
  repo: string | null | undefined;
  /** Where those links open. Absent means GitHub. */
  forge?: ForgeHost;
  /** The step the ticket has stopped on, whose link blinks. Null while it is simply running. */
  held: Held | null;
  activeKey: string | null;
  onActiveKeyChange: (key: string) => void;
  /** What a run was spawned on, read off `agent_spawned` — not `run.runtime_kind`. */
  runtimeFor: (run: StepRun) => Spawn | undefined;
  onOpenText: (content: LongContent, subtitle: string) => void;
  /** What a block's run can open in the Output drawer, or undefined when it has no terminal or
   *  preview. */
  outputOpener?: (segment: ActivitySegment) => OutputOpeners | undefined;
  /** Absent when the caller offers no interrupt; the button is then not rendered. */
  onInterrupt?: (stepRunId: number) => void;
  /** The failed run a Retry would follow (`retryOffer`), offered in that run's own block, while
   *  its backoff runs or once it escalated. Absent when there is none, or when the caller offers
   *  no retry. */
  retry?: RetryOffer;
  /** Force complete for the ticket's latest run (`forceOffer`), offered in that run's block. */
  forceComplete?: ForceCompleteProps;
  /** Redo, offered in the block of the run named by `runId`: the current step's latest. */
  redo?: RedoOffer;
  interrupting?: boolean;
  /**
   * What a block's step is waiting on, or null. Supplied by the caller because the answer is in
   * the PINNED CONFIG, not in the event log — a step that has not started has not written
   * anything, which is the whole reason the line exists.
   */
  waitingOn?: (segment: ActivitySegment) => Waiting | null;
  /** The entered-but-unstarted block, if any. Its header has no run to describe, and without a
   *  word for that the absent `interrupt` reads as missing rather than not-yet. */
  startingKey?: string | null;
  /** What that block's step will run on, from the pinned config. Null for a step that runs on no
   *  runtime, such as a gate. */
  startingRuntime?: Spawn | null;
  /** A new value takes the region to its latest event: the top newest first, the foot oldest
   *  first. */
  toLatest?: number | null;
  /** Leave out the routine rows (`isRoutine`). */
  focused?: boolean;
  /** What the block named by `key` asks of the reader, drawn under its header: the messages
   *  about the step the ticket waits at. */
  message?: { key: string; content: ReactNode };
}
export function ActivityPanel({
  segments,
  order,
  repo,
  forge,
  held,
  activeKey,
  onActiveKeyChange,
  runtimeFor,
  onOpenText,
  outputOpener,
  onInterrupt,
  interrupting,
  retry,
  forceComplete,
  redo,
  waitingOn,
  startingKey,
  startingRuntime,
  toLatest = null,
  focused = false,
  message,
}: ActivityPanelProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const nodeRefs = useRef(new Map<string, HTMLElement>());
  const programmaticRef = useRef(false);
  const settleRef = useRef(0);
  /**
   * The last key this component itself reported from a scroll. The scroll-into-view effect below
   * watches the same `activeKey` that effect causes to change, so without this it fights the
   * reader: you scroll, the probe reports a new block, and the effect snaps that block flush to
   * the probe line. Only a key that did NOT come from scrolling is worth scrolling to.
   */
  const reportedRef = useRef<string | null>(null);
  // The set-aside blocks a reader opened. A redo's discarded attempts are history, so their
  // events start folded away.
  const [unfolded, setUnfolded] = useState<ReadonlySet<string>>(() => new Set());
  // Over the whole log, not a block: the restart or exit that cancels a booking is filed elsewhere.
  const cancelled = useMemo(
    () => cancelledRetries(segments.flatMap((segment) => segment.entries)),
    [segments],
  );

  // Each block's rows as drawn, judged with the same `current` the row itself is drawn with.
  const shownBy = useMemo(() => {
    const shown = new Map<string, readonly TimelineEntry[]>();
    for (const segment of segments) {
      if (!focused) {
        shown.set(segment.key, segment.entries);
        continue;
      }
      const isCurrent = held?.key === segment.key ? currentRows(segment.entries) : null;
      shown.set(
        segment.key,
        segment.entries.filter((entry) => !isRoutine(factsFor(entry, isCurrent?.(entry) ?? false))),
      );
    }
    return shown;
  }, [segments, held, focused]);

  const setNodeRef = useCallback((key: string, el: HTMLElement | null) => {
    if (el) nodeRefs.current.set(key, el);
    else nodeRefs.current.delete(key);
  }, []);

  // Scroll -> which block am I reading -> tell the graph.
  useScrollSpy({
    root: scrollRef,
    keys: segments.map((segment) => segment.key),
    section: (key) => nodeRefs.current.get(key),
    offset: PROBE_OFFSET,
    paused: programmaticRef,
    onChange: (key) => {
      reportedRef.current = key;
      onActiveKeyChange(key);
    },
  });

  // Graph -> scroll here. A layout effect, so the jump happens before paint rather than as a
  // second visible movement after the node lights up.
  useLayoutEffect(() => {
    if (!activeKey || activeKey === reportedRef.current) return;
    const container = scrollRef.current;
    const el = nodeRefs.current.get(activeKey);
    if (!container || !el) return;

    // The region is now on this block, however it got here. Recording that is what lets the graph
    // send the reader BACK to a block they had once scrolled to — cycling a step's attempts comes
    // round to one of them sooner or later, and a stale `reportedRef` would swallow that jump.
    reportedRef.current = activeKey;

    const delta =
      el.getBoundingClientRect().top - container.getBoundingClientRect().top - PROBE_OFFSET;
    if (Math.abs(delta) < 2) return;

    programmaticRef.current = true;
    container.scrollTo({ top: container.scrollTop + delta, behavior: "smooth" });
    window.clearTimeout(settleRef.current);
    settleRef.current = window.setTimeout(() => {
      programmaticRef.current = false;
    }, 400);
  }, [activeKey]);

  // Flipping the order is a request to read from the other end, so go there. Deliberately NOT
  // programmatic: the probe effect above re-runs on the new `segments` array and reports whatever
  // now sits at the top, which is what keeps the graph's selection honest after the flip.
  const orderRef = useRef(order);
  useLayoutEffect(() => {
    if (orderRef.current === order) return;
    orderRef.current = order;
    scrollRef.current?.scrollTo({ top: 0 });
  }, [order]);

  // Not programmatic either, for the same reason: the graph follows to the block it lands on.
  const latestRef = useRef(toLatest);
  useLayoutEffect(() => {
    if (toLatest === null || latestRef.current === toLatest) return;
    latestRef.current = toLatest;
    const container = scrollRef.current;
    container?.scrollTo({
      top: order === "newest" ? 0 : container.scrollHeight,
      behavior: "smooth",
    });
  }, [toLatest, order]);

  // A switch of Focused keeps the reader in the block they were reading: rows come and go above
  // it, so the block goes back to the probe line. Not programmatic, so the graph stays on it.
  const focusedRef = useRef(focused);
  useLayoutEffect(() => {
    if (focusedRef.current === focused) return;
    focusedRef.current = focused;
    const container = scrollRef.current;
    const el = activeKey ? nodeRefs.current.get(activeKey) : undefined;
    if (!container || !el) return;
    container.scrollTop +=
      el.getBoundingClientRect().top - container.getBoundingClientRect().top - PROBE_OFFSET;
  }, [focused, activeKey]);

  useEffect(() => () => window.clearTimeout(settleRef.current), []);

  if (!segments.length) {
    return (
      <EmptyState
        header="Nothing has happened on this ticket yet"
        description="Its first step starts when its preconditions hold."
      />
    );
  }

  return (
    // As tall as the panel around it, which the page's fill layout sizes to end at the foot of
    // the window; the log scrolls inside it and the page does not.
    <div className="flex min-h-0 flex-1">
      <AttemptRail
        segments={segments}
        shownBy={shownBy}
        activeKey={activeKey}
        onPick={onActiveKeyChange}
      />

      <div ref={scrollRef} className="min-w-0 flex-1 overflow-y-auto overscroll-contain">
        {segments.map((segment) => {
          const segmentHeld = held?.key === segment.key ? held : null;
          const isCurrent = currentRows(segment.entries);
          const folded = Boolean(segment.run?.superseded_at) && !unfolded.has(segment.key);
          const shown = shownBy.get(segment.key) ?? segment.entries;
          const hidden = segment.entries.length - shown.length;
          return (
            <section key={segment.key} ref={(el) => setNodeRef(segment.key, el)}>
              <SegmentHeader
                segment={segment}
                runtimeFor={runtimeFor}
                output={outputOpener?.(segment)}
                onInterrupt={onInterrupt}
                interrupting={interrupting}
                retry={retry && segment.run?.id === retry.run.id ? retry : undefined}
                forceComplete={
                  forceComplete && segment.run?.id === forceComplete.run.id
                    ? forceComplete
                    : undefined
                }
                redo={redo && segment.run?.id === redo.runId ? redo : undefined}
                starting={segment.key === startingKey}
                hidden={hidden}
                startingRuntime={startingRuntime}
              />
              {message?.key === segment.key && message.content}
              {segment.entries.length === 0 ? (
                <p className="px-4 py-2 text-body-sm text-subtlest">
                  No events recorded for this attempt yet.
                </p>
              ) : shown.length === 0 ? null : folded ? (
                <div className="px-4 py-2">
                  <Button
                    spacing="compact"
                    onClick={() => setUnfolded((keys) => new Set(keys).add(segment.key))}
                  >
                    Show {shown.length} {shown.length === 1 ? "event" : "events"}
                  </Button>
                </div>
              ) : (
                <ol>
                  {shown.map((entry) => (
                    <EventRow
                      key={entry.id}
                      entry={entry}
                      repo={repo}
                      forge={forge}
                      held={segmentHeld}
                      current={segmentHeld !== null && isCurrent(entry)}
                      cancelled={cancelled.get(entry.id)}
                      onOpenText={(content) => onOpenText(content, subtitleFor(segment))}
                    />
                  ))}
                </ol>
              )}
              <WaitingOn waiting={waitingOn?.(segment) ?? null} />
            </section>
          );
        })}
        {/* Lets the last block reach the probe line, so the rail can highlight it. */}
        <div aria-hidden style={{ height: "70%" }} />
      </div>
    </div>
  );
}

/**
 * What the step is waiting on, when nothing has happened on it yet.
 *
 * The orchestrator cannot say this in the log: an unmet precondition declines silently by design,
 * because emitting would write one row per waiting step every 30 seconds
 * (`worker/agent_steps.py::_start_attempt`). The conditions are in the pinned snapshot, so they
 * are named — and only named: the API words them (`TicketDetail.waiting_on`), and an API from
 * before it did leaves the page the calls as config wrote them. Which one is false is a
 * server-side evaluation against live GitHub state, and guessing at it here would be inventing an
 * answer the client cannot have.
 */
function WaitingOn({ waiting }: { waiting: Waiting | null }) {
  if (waiting?.needs.length) {
    return (
      <p className="px-4 pb-2 text-body-sm text-subtle">
        {`Waiting for: ${waiting.needs.join("; ")}`}
      </p>
    );
  }
  if (!waiting?.calls.length) return null;
  return (
    <p className="px-4 pb-2 text-body-sm text-subtle">
      Waiting for the condition: <Code>{waiting.calls.join(", ")}</Code>
    </p>
  );
}

function subtitleFor(segment: ActivitySegment): string {
  return segment.attempt === null ? segment.step : `${segment.step}, attempt ${segment.attempt}`;
}

/**
 * The orientation indicator. A progress bar can only say how far through you are; this says how
 * far AND which attempt of which step you are in, which is the question actually being asked.
 */
function AttemptRail({
  segments,
  shownBy,
  activeKey,
  onPick,
}: {
  segments: ActivitySegment[];
  /** The rows each block draws, which the counts follow. */
  shownBy: ReadonlyMap<string, readonly TimelineEntry[]>;
  activeKey: string | null;
  onPick: (key: string) => void;
}) {
  return (
    <JumpRail
      label="Attempts"
      // Not on a phone, where it would take half the width from the log; the graph above it
      // picks an attempt there.
      className="w-44 shrink-0 overflow-y-auto border-r border-default bg-surface-sunken p-2 max-md:hidden"
      activeKey={activeKey}
      onPick={onPick}
      items={segments.map((segment) => {
        const count = (shownBy.get(segment.key) ?? segment.entries).length;
        return {
          key: segment.key,
          content: (
            <span className="min-w-0 flex-1">
              <span className="block truncate font-mono text-body-sm">
                {segment.step}
                {segment.attempt !== null && segment.attempt > 1 && (
                  <span className="text-subtlest"> #{segment.attempt}</span>
                )}
              </span>
              <span className="mt-0.5 flex items-center gap-1.5 font-normal">
                <span
                  role="img"
                  aria-label={
                    segment.status === "info" ? "Admission" : statusFacts(segment.status).label
                  }
                  className={cx("size-1.5 shrink-0 rounded-full", statusDot(segment.status))}
                />
                <span className="truncate text-body-sm text-subtlest">
                  {count} {count === 1 ? "event" : "events"}
                </span>
              </span>
            </span>
          ),
        };
      })}
    />
  );
}
