/**
 * One position in the stack of project bands: a project gutter, then one grid per lane.
 *
 * The band is a position, not a project. Which project it shows changes when the modes re-rank
 * them, and that swap happens while the board is muted, so the label is never seen changing
 * mid-flight. Nothing here is positioned or sized in pixels: every lane is a CSS grid of
 * fixed-size cells, which is what guarantees a square of the screen stays exactly where it is.
 *
 * Three click targets, three focus kinds — the gutter takes the row, a cell takes the
 * intersection, and the lane headings above (in `Board`) take the column.
 */

import { plural } from "../../format";
import { Metric } from "../ui";
import { cornersAt, type Focus, type FocusGeometry, gutterFocused } from "./focus";
import { type BandLayout, COLS } from "./layout";
import { issueNumber } from "./list";
import {
  formatMetric,
  LANE_LABELS,
  type Lane,
  METRIC_SPECS,
  type Mode,
  metricOf,
  retriesOf,
} from "./model";
import { Reticle } from "./Reticle";
import type { Slot } from "./slots";

export interface BandProps {
  band: BandLayout;
  bandIndex: number;
  lanes: Lane[];
  rows: number;
  mode: Mode;
  /** This band's slice of the board, in canonical order. */
  slots: Slot[];
  /** Index of this band's first slot in the flat board array — `fading` is keyed on that. */
  base: number;
  fading: Set<number>;
  /** True only while THIS band's project is being swapped out by a re-rank — never merely
   *  because a crossfade is running, or every label on the board would blink on each click. */
  swapping: boolean;
  geometry: FocusGeometry | null;
  /** The second argument is the ticket whose square was clicked, or null for the cell itself. */
  onFocus: (focus: Focus, ticket?: number | null) => void;
  /** The square under the pointer and what it holds, or null once the pointer leaves the cell. */
  onTip?: (anchor: HTMLElement | null, text: string) => void;
  /** Bands on the board, so a column focus knows which cells are its ends. */
  bandCount: number;
  /** One node group's box in px, for the reticle overlay. Nothing else here uses pixels. */
  cellWidth: number;
  cellHeight: number;
}

export function Band({
  band,
  bandIndex,
  lanes,
  rows,
  mode,
  slots,
  base,
  fading,
  swapping,
  geometry,
  onFocus,
  onTip,
  bandCount,
  cellWidth,
  cellHeight,
}: BandProps) {
  const perLane = rows * COLS;
  const gutterOn = gutterFocused(geometry, bandIndex);

  return (
    <section className="factory-board__band">
      <button
        type="button"
        className="factory-board__gutter"
        data-fading={swapping || undefined}
        data-focused={gutterOn || undefined}
        aria-pressed={gutterOn}
        onClick={() => onFocus({ kind: "project", projectId: band.project.id })}
      >
        <span className="factory-board__gutter-name">{band.project.name}</span>
        <span className="factory-board__gutter-count">
          <Metric>{summarise(band, mode)}</Metric>
        </span>
      </button>

      {lanes.map((lane, laneIndex) => {
        const cell = slots.slice(laneIndex * perLane, (laneIndex + 1) * perLane);
        const held = cell.filter((slot) => slot.node !== null).length;
        const corners = cornersAt(geometry, bandIndex, laneIndex, bandCount, lanes.length);
        return (
          <button
            key={lane}
            type="button"
            className="factory-board__lane"
            style={{ ["--fb-lane" as string]: laneIndex }}
            data-focused={corners !== null || undefined}
            aria-label={`${band.project.name}, ${LANE_LABELS[lane]}, ${plural(held, "ticket")}`}
            // A cell with nothing in it has no focus to offer: the list it would fill is empty,
            // and the pips would bracket bare lattice. Out of the tab order as well, or the
            // empty cells would be most of the tab stops on the board.
            //
            // `aria-disabled` and NOT the `disabled` attribute, which looks like the obvious
            // choice and is a trap: the theme gives a disabled BUTTON an inset border so it
            // still reads as a control (`theme/dark.css`), and a lane cell is exactly as wide
            // as its node group — so that border drew a box tight around the nodes on every
            // empty cell. This cell is a picture, not an unavailable control.
            aria-disabled={held === 0 || undefined}
            tabIndex={held === 0 ? -1 : undefined}
            // The squares' hover text, read off the event rather than bound per square. Mouse
            // only, and never the only place it lives: the issue list says all of it in words.
            onPointerOver={(event) => {
              const hit = (event.target as HTMLElement).closest<HTMLElement>("[data-ticket]");
              const id = hit === null ? null : Number(hit.getAttribute("data-ticket"));
              const slot = id === null ? undefined : cell.find((each) => each.node?.id === id);
              onTip?.(slot ? hit : null, slot ? (describe(slot, lane, mode) ?? "") : "");
            }}
            onPointerLeave={() => onTip?.(null, "")}
            // Which square was hit, if any. The slots are spans inside this button rather than
            // buttons of their own — a button inside a button is not a thing, and two thousand
            // of them would be two thousand tab stops — so the cell reads its own event to find
            // out whether a reader aimed at a ticket or at the space around one. A keyboard
            // Enter lands on the button itself and therefore means the cell, which is right.
            onClick={(event) => {
              // `pointer-events: none` already stops this, and the cell is out of the tab order;
              // this is the guard for anything that activates a button programmatically.
              if (held === 0) return;
              const hit = (event.target as HTMLElement).closest("[data-ticket]");
              onFocus(
                { kind: "cell", lane, projectId: band.project.id },
                hit === null ? null : Number(hit.getAttribute("data-ticket")),
              );
            }}
          >
            {/* The node group is its own box inside the wider, clickable lane cell, so the
                hover rectangle and the pips hug the squares rather than the empty space beside
                them. */}
            <span className="factory-board__group">
              {corners !== null && (
                <Reticle width={cellWidth} height={cellHeight} corners={corners} />
              )}
              {cell.map((slot, offset) => {
                const at = base + laneIndex * perLane + offset;
                return (
                  <span
                    key={at}
                    className="factory-board__slot"
                    data-look={slot.appearance}
                    data-ticket={slot.node?.id}
                    data-fading={fading.has(at) || undefined}
                  />
                );
              })}
            </span>
          </button>
        );
      })}
    </section>
  );
}

/**
 * A band position with no project in it.
 *
 * The board reaches the bottom of its panel whether or not there are enough projects to fill it,
 * and what fills the rest is the same lattice drawn hollow: outlines where a filled slot would
 * be. It says "room for another repo here" in the board's own vocabulary, where blank paper would
 * have said the board had simply ended and left the panel too tall.
 *
 * Inert, and `aria-hidden`: there is nothing here to click, focus, or read out. Only the shape of
 * a thing that is not there.
 */
export function VacantBand({ lanes, rows }: { lanes: Lane[]; rows: number }) {
  const cells = rows * COLS;
  return (
    <section className="factory-board__band factory-board__band--vacant" aria-hidden="true">
      <div className="factory-board__gutter-blank" />
      {lanes.map((lane) => (
        <div key={lane} className="factory-board__lane">
          <span className="factory-board__group">
            {Array.from({ length: cells }, (_, offset) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: a slot IS its position, and nothing here ever reorders
              <span key={offset} className="factory-board__slot factory-board__slot--vacant" />
            ))}
          </span>
        </div>
      ))}
    </section>
  );
}

/**
 * The hover text on one square. Exported for its test: the bubble only exists under a pointer.
 *
 * A shade mode appends its own metric, because the shade alone only says which of four bands a
 * ticket landed in — the tooltip is where the actual number lives.
 */
export function describe(slot: Slot, lane: Lane, mode: Mode): string | undefined {
  const node = slot.node;
  if (!node) return undefined;
  const suffix = retriesOf(node) > 0 ? ` · attempt ${node.attempt}` : "";
  const metric = metricOf(mode);
  const spend = metric === null ? "" : `\n${formatMetric(node, metric)}`;
  const title = node.title ? `\n${node.title}` : "";
  const step = node.process ? `${node.process}/${node.step}` : node.step;
  return `${issueNumber(node.key)}${title}\n${step} · ${LANE_LABELS[lane]}${suffix}${spend}`;
}

/** The gutter's second line: whatever the active mode makes this band worth looking at for. */
function summarise(band: BandLayout, mode: Mode): string {
  // The band's own count, not the drawn one: `MAX_ROWS` can leave nodes off the lattice, and the
  // gutter is the only thing on the board that says how many tickets a project actually has.
  const total = band.total;
  const metric = metricOf(mode);
  if (metric !== null)
    return `${METRIC_SPECS[metric].formatSum(band.load)} · ${plural(total, "ticket")}`;
  return plural(total, "ticket");
}
