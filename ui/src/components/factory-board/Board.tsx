/**
 * The factory board, and the crossfade that carries it between modes.
 *
 * Nothing here moves. A mode change is a two-beat colour transition instead: every position whose
 * appearance would change fades to the muted tone, the new layout is applied underneath while
 * they are all muted, and they fade back in to whatever they now show. Positions that look the
 * same in both layouts — two running nodes trading places — are left alone entirely, which is
 * what keeps the effect surgical rather than a full-board strobe on every hover.
 *
 * The swap is timed rather than event-driven because `transitionend` on a couple of thousand
 * elements is a stampede, and the two durations are ours to pick anyway. `--fb-sweep` staggers
 * the fade per lane so the change reads as a sweep down the pipeline; the swap waits for the
 * last lane's delay before committing, or the tail lanes would jump instead of fading.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnchoredTooltip, Icon, prefersReducedMotion, Tooltip } from "../ui";
import { Band, VacantBand } from "./Band";
import { type Focus, type FocusGeometry, geometryOf, laneHeadFocused } from "./focus";
import { COLS, type LaneSort, layout } from "./layout";
import {
  countLanes,
  DEFAULT_THRESHOLDS,
  type IssueNode,
  LANE_HINTS,
  LANE_LABELS,
  type Lane,
  type Mode,
  type ProjectBand,
  type Thresholds,
} from "./model";
import { changedSlots, type Slot, slotsOf } from "./slots";

/**
 * Fade out, then fade in — at one of two speeds, matching the `--fb-dissolve` / `--fb-settle` /
 * `--fb-sweep` blocks the stylesheet keys off `data-pace`.
 *
 * A focus change lands on a deliberate click, so it can be brisk: the reader already knows what
 * they asked for and only needs to see it arrive. A mode change can arrive from a hover — every
 * shading button on the rail previews on one — where the same speed would feel twitchy, so it
 * keeps the longer beat.
 */
const PACE = {
  click: { dissolve: 45, sweep: 3 },
  hover: { dissolve: 110, sweep: 8 },
} as const;

type Pace = keyof typeof PACE;

const NOTHING: Set<number> = new Set();

/**
 * How many band positions fit in a box, given one band's height and the gap between them.
 *
 * `floor`, because a band that only half fits is a band the reader would have to scroll to see —
 * and the board's panel is sized to the bottom of the window precisely so that nothing has to be.
 */
export function bandCapacity(available: number, band: number, gap: number): number {
  if (band <= 0) return 0;
  return Math.max(0, Math.floor((available + gap) / (band + gap)));
}

/**
 * How many bands the board has room for, measured rather than calculated.
 *
 * Every number here is read off the rendered board — one band's height, the gap between them, the
 * space left under the lane headings — because every one of them is a stylesheet's business:
 * `--fb-node` and `--fb-gap` move with the density switch, the band gap is a `rem`, and the row
 * count moves with the busiest lane on the board. Deriving it instead would mean keeping a copy
 * of all of that in TypeScript and a test to hold the two together.
 *
 * Adding bands changes the height of the box being observed, which sounds like a loop and is not:
 * the measurement is taken from the container's TOP and one band's height, and neither moves when
 * bands are added below.
 */
function useBandFill(
  box: React.RefObject<HTMLElement | null>,
  bands: React.RefObject<HTMLElement | null>,
) {
  const [fits, setFits] = useState(0);

  useEffect(() => {
    const scroll = box.current;
    const list = bands.current;
    if (scroll === null || list === null || typeof ResizeObserver === "undefined") return;

    const measure = () => {
      const first = list.firstElementChild;
      if (!(first instanceof HTMLElement)) return setFits(0);
      const gap = Number.parseFloat(getComputedStyle(list).rowGap) || 0;
      const pad = Number.parseFloat(getComputedStyle(scroll).paddingBottom) || 0;
      // Where the bands begin INSIDE the scrolled content, not on the screen. The two are the
      // same until somebody scrolls, and then a viewport measurement would report the bands as
      // starting higher and higher — and answer by adding vacant rows to a board that is already
      // too tall for its panel.
      const top =
        list.getBoundingClientRect().top - scroll.getBoundingClientRect().top + scroll.scrollTop;
      const available = scroll.clientHeight - top - pad;
      setFits(bandCapacity(available, first.getBoundingClientRect().height, gap));
    };

    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(scroll);
    observer.observe(list);
    return () => observer.disconnect();
  }, [box, bands]);

  return fits;
}

export interface BoardProps {
  nodes: IssueNode[];
  projects: ProjectBand[];
  lanes: Lane[];
  mode: Mode;
  /** What a reader has clicked into, or null. Everything outside it mutes. */
  focus: Focus | null;
  /** The second argument is the ticket whose square was clicked, or null for anything else. */
  onFocus: (focus: Focus, ticket?: number | null) => void;
  /** Which lane heading is ranking the project rows, if any. */
  laneSort: LaneSort | null;
  /** A lane heading's sort was pressed. Null is "back to the mode's own order". */
  onLaneSort: (sort: LaneSort | null) => void;
  /** Where the active shade mode's ramp turns. Read by nothing else on the board. */
  thresholds?: Thresholds;
  nodeSize: number;
  gap: number;
}

export function Board({
  nodes,
  projects,
  lanes,
  mode,
  focus,
  onFocus,
  laneSort,
  onLaneSort,
  thresholds = DEFAULT_THRESHOLDS,
  nodeSize,
  gap,
}: BoardProps) {
  const target = useMemo(
    () => layout(nodes, projects, lanes, mode, laneSort),
    [nodes, projects, lanes, mode, laneSort],
  );
  const targetSlots = useMemo(
    () => slotsOf(target, mode, focus, thresholds),
    [target, mode, focus, thresholds],
  );
  const counts = useMemo(() => countLanes(nodes), [nodes]);
  const { board, slots, shownMode, fading, pace } = useDissolve(
    target,
    targetSlots,
    mode,
    lanes.length,
  );

  const perBand = lanes.length * board.rows * COLS;
  // The reticle is an SVG overlay, so it is the one thing on this board that needs the node
  // group's size in pixels. Nothing is positioned by it — the squares are still a CSS grid.
  const pitch = nodeSize + gap;
  const cellWidth = COLS * pitch - gap;
  const cellHeight = board.rows * pitch - gap;
  // Resolved against the layout being SHOWN, not the target: while a crossfade is mid-flight the
  // bands on screen are still the old ones, and pips drawn on the new row would sit on the
  // wrong project for the length of the fade.
  const scroll = useRef<HTMLDivElement>(null);
  const bands = useRef<HTMLDivElement>(null);
  const fits = useBandFill(scroll, bands);
  const geometry: FocusGeometry | null = useMemo(
    () =>
      geometryOf(
        focus,
        lanes,
        board.bands.map((band) => band.project.id),
      ),
    [focus, lanes, board],
  );
  // The square under the pointer, and what to say about it. One bubble for the whole board, from
  // one delegated hover per cell, rather than a tooltip on each of a couple of thousand squares.
  // Keyed to the mode it was read in: a re-rank can put another ticket under a still pointer.
  const [tip, setTip] = useState<{ anchor: HTMLElement; text: string; mode: Mode } | null>(null);
  const onTip = useCallback(
    (anchor: HTMLElement | null, text: string) =>
      setTip(anchor ? { anchor, text, mode: shownMode } : null),
    [shownMode],
  );
  // Memoised apart from `tip`, so a hover that only moves the tooltip hands React the same
  // element tree and leaves the lattice of every band unreconciled.
  const bandRows = useMemo(
    () => (
      <>
        {board.bands.map((band, index) => (
          // Keyed by POSITION, not by project: the band is a fixed place on the screen and the
          // project ranked into it is what changes. Keying by project id would make React move
          // the elements, which is the one thing this design forbids.
          <Band
            // biome-ignore lint/suspicious/noArrayIndexKey: the index is the identity here, a band being a fixed position rather than a project
            key={index}
            band={band}
            bandIndex={index}
            lanes={lanes}
            rows={board.rows}
            mode={shownMode}
            slots={slots.slice(index * perBand, (index + 1) * perBand)}
            base={index * perBand}
            fading={fading}
            // Only when a re-rank is actually swapping this band's project. Fading every
            // gutter whenever anything crossfades made the project labels blink on each click.
            swapping={
              fading.size > 0 && board.bands[index]?.project.id !== target.bands[index]?.project.id
            }
            geometry={geometry}
            onFocus={onFocus}
            onTip={onTip}
            bandCount={board.bands.length}
            cellWidth={cellWidth}
            cellHeight={cellHeight}
          />
        ))}
        {/* Whatever is left of the panel, in the board's own lattice. Rendered after the real
            bands and never mixed in with them: a vacant position is not a project that happens
            to be empty, and the re-ranking must never sort one into the middle of the board. */}
        {Array.from({ length: Math.max(0, fits - board.bands.length) }, (_, index) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: the index is the identity here, a band being a fixed position rather than a project
          <VacantBand key={index} lanes={lanes} rows={board.rows} />
        ))}
      </>
    ),
    [
      board,
      slots,
      shownMode,
      fading,
      target,
      geometry,
      onFocus,
      onTip,
      lanes,
      perBand,
      cellWidth,
      cellHeight,
      fits,
    ],
  );

  return (
    <div className="factory-board__scroll" ref={scroll}>
      <div
        className="factory-board"
        data-mode={shownMode}
        data-pace={pace}
        style={{ ["--fb-node" as string]: `${nodeSize}px`, ["--fb-gap" as string]: `${gap}px` }}
      >
        {/* Painted by `--fb-ground`, which is the SHELL's own ground rather than a colour picked
            to match it: the panel under this is unpainted, so the strip and what it covers are
            the same surface by construction, in either theme. A sticky header has to be opaque,
            and a second declaration of the same colour is a second thing to keep in step. */}
        <div className="factory-board__lanes">
          {lanes.map((lane, index) => {
            const sorted = laneSort?.lane === lane ? laneSort.dir : null;
            return (
              // A heading does two things now, so it is a pair of buttons rather than one: the
              // name focuses the column, the caret ranks the project rows by it. Nested buttons
              // are not a thing, and folding the sort into the focus click would cost the reader
              // the ability to ask either question without also asking the other.
              <div
                key={lane}
                className="factory-board__lane-head"
                data-focused={laneHeadFocused(geometry, index) || undefined}
              >
                <Tooltip content={LANE_HINTS[lane]} describes>
                  <button
                    type="button"
                    className="factory-board__lane-pick"
                    aria-pressed={laneHeadFocused(geometry, index)}
                    onClick={() => onFocus({ kind: "lane", lane })}
                  >
                    <span className="factory-board__lane-name">{LANE_LABELS[lane]}</span>
                    <span className="factory-board__lane-count">{counts[lane]}</span>
                  </button>
                </Tooltip>
                {/* Three states in one press each: off, most in this lane first, fewest first,
                    and round again — the same cycle a sortable table heading has, so nobody has
                    to find a second control to get back out of it. */}
                <Tooltip
                  describes
                  content={
                    sorted === null
                      ? `Rank the projects by how many are in ${LANE_LABELS[lane]}`
                      : sorted === "desc"
                        ? "Most first. Press for fewest first."
                        : "Fewest first. Press to stop sorting."
                  }
                >
                  <button
                    type="button"
                    className="factory-board__lane-sort"
                    data-on={sorted !== null || undefined}
                    aria-pressed={sorted !== null}
                    aria-label={`Sort projects by ${LANE_LABELS[lane]} tickets`}
                    onClick={() =>
                      onLaneSort(
                        sorted === null
                          ? { lane, dir: "desc" }
                          : sorted === "desc"
                            ? { lane, dir: "asc" }
                            : null,
                      )
                    }
                  >
                    <Icon glyph={sorted === "asc" ? "arrow-up" : "arrow-down"} size="small" />
                  </button>
                </Tooltip>
              </div>
            );
          })}
        </div>

        <div className="factory-board__bands" ref={bands}>
          {bandRows}
        </div>
      </div>
      {tip !== null && tip.mode === shownMode && (
        <AnchoredTooltip anchor={tip.anchor} content={tip.text} />
      )}
    </div>
  );
}

interface Shown {
  board: ReturnType<typeof layout>;
  slots: Slot[];
  mode: Mode;
}

function useDissolve(
  target: ReturnType<typeof layout>,
  targetSlots: Slot[],
  mode: Mode,
  laneCount: number,
) {
  const [shown, setShown] = useState<Shown>({ board: target, slots: targetSlots, mode });
  const [fading, setFading] = useState<Set<number>>(NOTHING);
  const [pace, setPace] = useState<Pace>("click");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (shown.slots === targetSlots) return;

    const changed = changedSlots(shown.slots, targetSlots);
    const settle = () => setShown({ board: target, slots: targetSlots, mode });

    // Nothing to fade, or the reader has asked for no motion: commit straight away.
    if (changed.size === 0 || prefersReducedMotion()) {
      setFading(NOTHING);
      settle();
      return;
    }

    // A mode change may have come from a hover; a focus change is always a click.
    const next: Pace = mode === shown.mode ? "click" : "hover";
    setPace(next);
    setFading(changed);
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = setTimeout(
      () => {
        timer.current = null;
        setFading(NOTHING);
        settle();
      },
      // The swap waits out the last lane's stagger, or the tail lanes would jump rather than fade.
      PACE[next].dissolve + Math.max(laneCount - 1, 0) * PACE[next].sweep,
    );

    return () => {
      if (timer.current !== null) clearTimeout(timer.current);
      timer.current = null;
    };
  }, [target, targetSlots, mode, laneCount, shown.slots, shown.mode]);

  return { board: shown.board, slots: shown.slots, shownMode: shown.mode, fading, pace };
}
