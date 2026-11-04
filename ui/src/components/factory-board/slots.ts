/**
 * The board as a flat array of screen positions.
 *
 * This is what makes the crossfade possible. Nothing on this board moves, so a square of the
 * screen — band position, lane, row, column — is the stable thing, and the ticket it displays is
 * what changes underneath it. Index `i` is therefore the SAME square in every mode, and two
 * layouts can be compared position by position to find the ones that actually need to fade.
 *
 * Empty positions are in the array too. When the bands re-rank, a position that held a ticket
 * from a busy project can land on a quiet one and hold nothing — treating that as an appearance
 * change rather than as an element disappearing is what keeps it a fade instead of a pop.
 */

import { type Focus, inFocus } from "./focus";
import { type BoardLayout, COLS } from "./layout";
import {
  appearanceOf,
  DEFAULT_THRESHOLDS,
  EMPTY,
  type IssueNode,
  type Mode,
  type Thresholds,
} from "./model";

export interface Slot {
  bandIndex: number;
  laneIndex: number;
  row: number;
  col: number;
  /** `empty`, `muted`, a ticket status, or `heat-N`. The one token the stylesheet reads. */
  appearance: string;
  node: IssueNode | null;
}

/**
 * `focus` is the lane, project, or cell a reader has clicked into. Everything outside it mutes,
 * and entering a focus is a crossfade like any other view change.
 *
 * `thresholds` only reaches `appearanceOf`, and only a shade mode reads it. Moving a threshold
 * therefore changes appearance tokens and nothing else, so it crossfades like any other view
 * change rather than relaying out the board.
 */
export function slotsOf(
  board: BoardLayout,
  mode: Mode,
  focus: Focus | null = null,
  thresholds: Thresholds = DEFAULT_THRESHOLDS,
): Slot[] {
  const perLane = board.rows * COLS;
  const perBand = board.lanes.length * perLane;
  const slots: Slot[] = new Array(board.bands.length * perBand);

  for (let bandIndex = 0; bandIndex < board.bands.length; bandIndex += 1) {
    for (let laneIndex = 0; laneIndex < board.lanes.length; laneIndex += 1) {
      for (let row = 0; row < board.rows; row += 1) {
        for (let col = 0; col < COLS; col += 1) {
          slots[bandIndex * perBand + laneIndex * perLane + row * COLS + col] = {
            bandIndex,
            laneIndex,
            row,
            col,
            appearance: EMPTY,
            node: null,
          };
        }
      }
    }
  }

  for (const [bandIndex, band] of board.bands.entries()) {
    for (const placement of band.placements) {
      const at =
        bandIndex * perBand + placement.laneIndex * perLane + placement.row * COLS + placement.col;
      const slot = slots[at];
      if (!slot) continue;
      slot.node = placement.node;
      slot.appearance = inFocus(focus, placement.node)
        ? appearanceOf(placement.node, mode, thresholds)
        : "muted";
    }
  }

  return slots;
}

/**
 * The positions that look different between two layouts — the set the transition fades.
 *
 * Driven off `after`, so a board that changed size is not compared out of alignment: every
 * position in the new board with no counterpart is simply new.
 */
export function changedSlots(before: Slot[], after: Slot[]): Set<number> {
  const changed = new Set<number>();
  for (let i = 0; i < after.length; i += 1) {
    if (before[i]?.appearance !== after[i]?.appearance) changed.add(i);
  }
  return changed;
}
