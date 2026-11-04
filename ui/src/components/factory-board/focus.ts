/**
 * What a reader has clicked into, and what that lights up.
 *
 * Three kinds, one per click target: a lane heading focuses a column, a project gutter focuses a
 * row, and a node group — the cell where one project meets one lane — focuses their intersection.
 *
 * Two separate things follow from a focus, and keeping them apart is what makes this readable:
 *
 *   - `inFocus` decides which NODES keep their colour. Everything else mutes, through the same
 *     crossfade every other view change uses.
 *   - `cornersAt` decides which CELLS get which corner pips — the same selection reticle the process
 *     graph draws around a chosen node (`components/flow/flowSelect.ts`). One rule covers all
 *     three kinds: the pips are the corners of a single rectangle enclosing the focused set, so
 *     a row or column splits its four corners between its two end groups.
 *
 * The focused project and lane are named in cyan rather than boxed, so the two headings and the
 * pips answer different questions: the pips say where, the colour says what.
 */

import { type IssueNode, LANE_LABELS, type Lane, laneOf, type ProjectBand } from "./model";

export type Focus =
  | { kind: "lane"; lane: Lane }
  | { kind: "project"; projectId: number }
  | { kind: "cell"; lane: Lane; projectId: number };

/**
 * The focus in words, for the applied-state row and the issue list's heading — one wording, so
 * the two panels name one selection the same way. A cell is the pair, project first and narrowing
 * rightwards, because that is exactly what it is: one lane, in one project.
 */
export function describeFocus(focus: Focus, projects: ProjectBand[]): string {
  const project = (id: number) => projects.find((p) => p.id === id)?.name ?? "Project";
  switch (focus.kind) {
    case "lane":
      return LANE_LABELS[focus.lane];
    case "project":
      return project(focus.projectId);
    default:
      return `${project(focus.projectId)} / ${LANE_LABELS[focus.lane]}`;
  }
}

/** Whether a node keeps its lane colour, or mutes. Everything is in focus when nothing is. */
export function inFocus(focus: Focus | null, node: IssueNode): boolean {
  if (focus === null) return true;
  switch (focus.kind) {
    case "lane":
      return laneOf(node) === focus.lane;
    case "project":
      return node.projectId === focus.projectId;
    default:
      return laneOf(node) === focus.lane && node.projectId === focus.projectId;
  }
}

/**
 * The focus resolved to board coordinates.
 *
 * Positions, not identities, because the bands re-rank: which row a project occupies is a
 * property of the current layout, and the rail has to be drawn where the project actually is.
 * `-1` means the axis is unconstrained — a lane focus pins no row, a project focus no column.
 */
export interface FocusGeometry {
  kind: Focus["kind"];
  band: number;
  lane: number;
}

export function geometryOf(
  focus: Focus | null,
  lanes: Lane[],
  bandProjectIds: number[],
): FocusGeometry | null {
  if (focus === null) return null;
  const lane = focus.kind === "project" ? -1 : lanes.indexOf(focus.lane);
  const band = focus.kind === "lane" ? -1 : bandProjectIds.indexOf(focus.projectId);
  // A focus naming a lane or project the board is not drawing lights nothing, rather than
  // lighting row -1 and column -1.
  if ((focus.kind !== "project" && lane === -1) || (focus.kind !== "lane" && band === -1)) {
    return null;
  }
  return { kind: focus.kind, band, lane };
}

/**
 * Corner flags in `selectCornerTriangles` order: top-left, top-right, bottom-left, bottom-right.
 */
export type Corners = readonly [boolean, boolean, boolean, boolean];

const ALL: Corners = [true, true, true, true];

/**
 * Which reticle corners this cell carries, if any.
 *
 * The four pips of a focus always describe ONE rectangle, whatever its extent. A cell focus draws
 * that rectangle around a single node group and takes all four. A column focus stretches it down
 * the lane, so the top group takes the two top corners and the bottom group the two bottom ones;
 * a row focus stretches it across, splitting left corners from right. The groups in between carry
 * nothing — the eye closes the rectangle for itself.
 *
 * A board only one band deep (or one lane wide) makes the same group both ends, and it correctly
 * takes all four.
 */
export function cornersAt(
  geometry: FocusGeometry | null,
  bandIndex: number,
  laneIndex: number,
  bandCount: number,
  laneCount: number,
): Corners | null {
  if (geometry === null) return null;
  switch (geometry.kind) {
    case "lane": {
      if (laneIndex !== geometry.lane) return null;
      const top = bandIndex === 0;
      const bottom = bandIndex === bandCount - 1;
      return top || bottom ? [top, top, bottom, bottom] : null;
    }
    case "project": {
      if (bandIndex !== geometry.band) return null;
      const left = laneIndex === 0;
      const right = laneIndex === laneCount - 1;
      return left || right ? [left, right, left, right] : null;
    }
    default:
      return bandIndex === geometry.band && laneIndex === geometry.lane ? ALL : null;
  }
}

/** Is this lane's heading part of the focus? True for a lane focus and for a cell focus. */
export function laneHeadFocused(geometry: FocusGeometry | null, laneIndex: number): boolean {
  return geometry !== null && geometry.lane === laneIndex;
}

/** Is this band's gutter part of the focus? True for a project focus and for a cell focus. */
export function gutterFocused(geometry: FocusGeometry | null, bandIndex: number): boolean {
  return geometry !== null && geometry.band === bandIndex;
}

/**
 * The nodes a focus selects; every node when nothing is focused, so the list has the whole board
 * to show before any selection is made.
 */
export function focusedNodes(focus: Focus | null, nodes: IssueNode[]): IssueNode[] {
  return nodes.filter((node) => inFocus(focus, node));
}

/** Does clicking this target turn the current focus off rather than moving it? */
export function samePlace(a: Focus | null, b: Focus): boolean {
  if (a === null) return false;
  // Each arm re-checks `a.kind`, which is also what narrows it — so there is deliberately no
  // outer kind guard here. One would read as a fast path and be pure dead code.
  switch (b.kind) {
    case "lane":
      return a.kind === "lane" && a.lane === b.lane;
    case "project":
      return a.kind === "project" && a.projectId === b.projectId;
    default:
      return a.kind === "cell" && a.lane === b.lane && a.projectId === b.projectId;
  }
}
