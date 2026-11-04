/**
 * Where every node sits, for a given mode. The whole of factory board's behaviour is this function.
 *
 * It returns grid coordinates — lane, column, row — and never pixels, so the density switch can
 * resize the board without re-running any of this, and so the tests can assert on ordering
 * instead of on arithmetic that depends on a CSS variable. `Board.tsx` multiplies by the pitch.
 *
 * Two rules carry the feature:
 *
 *   1. Every comparison ends in a tie-break on ticket id. Ranks change between modes; the order
 *      inside a rank must not, or the board churns on every hover and the motion stops meaning
 *      anything.
 *   2. A band is a rectangle. Its row count is the deepest of its lanes (at least `MIN_ROWS`),
 *      shared by all of them, so the empty lattice behind the nodes stays a clean grid.
 */

import {
  type IssueNode,
  type Lane,
  laneOf,
  type Metric,
  type Mode,
  metricOf,
  metricValue,
  type ProjectBand,
} from "./model";

/** Slots across a lane. Ten, so a full row reads as a round number at a glance. */
export const COLS = 10;

/** Rows every cell reserves however quiet the board is — the "4 rows of 10" floor. */
export const MIN_ROWS = 4;

/**
 * The deepest a band may get. Every band shares one row count, so without a cap a single project
 * with a few hundred finished tickets would make all of them that deep and push the rest of the
 * factory off the screen. The gutter reports the band's true total either way, so the cap changes
 * what is drawn and never what is claimed.
 */
export const MAX_ROWS = 12;

export interface Placement {
  node: IssueNode;
  laneIndex: number;
  col: number;
  row: number;
}

export interface BandLayout {
  project: ProjectBand;
  /**
   * How many tickets this project has in each lane, in `lanes` order — uncapped, like `total`.
   *
   * It is what the lane-heading sort ranks the bands by, and it has to be counted here because
   * `placements` has already been through `MAX_ROWS`: ranking on what is DRAWN would put the
   * project with a hundred blocked tickets below one with twelve.
   */
  laneTotals: number[];
  /**
   * Σ of the active mode's metric across the band — what a shade mode ranks bands by, and what
   * the gutter reports. 0 outside a shade mode, where no metric is selected and nothing reads it.
   */
  load: number;
  /**
   * Every node in the band, drawn or not — what the gutter reports. Not `placements.length`: the
   * `MAX_ROWS` cap can leave nodes undrawn, and a count that followed the cap would understate
   * the band exactly when it matters most.
   */
  total: number;
  placements: Placement[];
}

export interface BoardLayout {
  lanes: Lane[];
  /** Rows of slots every cell on the board reserves — at least `MIN_ROWS`. */
  rows: number;
  /** In display order: re-ranked by the active mode, declared order while idle. */
  bands: BandLayout[];
  /** Nodes actually placed. Excludes any dropped for naming an unknown lane or project. */
  totalNodes: number;
}

/**
 * A lane heading asked to rank the bands by how many tickets each project has in it.
 *
 * Null is the ordinary state, in which the mode decides — see `compareBands`. An explicit sort
 * outranks the mode's own ranking, because it is the one of the two the reader asked for by name.
 */
export interface LaneSort {
  lane: Lane;
  dir: "asc" | "desc";
}

export function layout(
  nodes: IssueNode[],
  projects: ProjectBand[],
  lanes: Lane[],
  mode: Mode,
  laneSort: LaneSort | null = null,
): BoardLayout {
  const laneIndex = new Map(lanes.map((lane, index) => [lane, index]));
  const rank = new Map(projects.map((project, index) => [project.id, index]));
  const metric = metricOf(mode);

  // Bucket first: a node is only ever placed in one (band, lane) cell, and everything after
  // this is per-cell work.
  const cells = new Map<string, IssueNode[]>();
  let totalNodes = 0;
  for (const node of nodes) {
    // `laneOf` is total, so the only thing that can drop a ticket here is a project the board
    // was not asked to draw. Nothing is ever swept into lane zero and silently misreported.
    const lane = laneIndex.get(laneOf(node));
    if (lane === undefined || !rank.has(node.projectId)) continue;
    const key = `${node.projectId}:${lane}`;
    const cell = cells.get(key);
    if (cell) cell.push(node);
    else cells.set(key, [node]);
    totalNodes += 1;
  }

  // One row count for every cell on the board: the deepest cell anywhere decides it. Taken
  // before the per-band work because a band cannot be sized from its own contents.
  let deepest = MIN_ROWS;
  for (const cell of cells.values()) deepest = Math.max(deepest, Math.ceil(cell.length / COLS));
  const rows = Math.min(MAX_ROWS, deepest);
  const capacity = rows * COLS;

  const bands: BandLayout[] = projects.map((project) => {
    const own = lanes.map((_, lane) => cells.get(`${project.id}:${lane}`) ?? []);
    let load = 0;
    let total = 0;
    const placements: Placement[] = [];

    for (const [lane, cell] of own.entries()) {
      const ordered = cell.slice().sort((a, b) => compareNodes(a, b, metric));
      for (const [slot, node] of ordered.entries()) {
        // Counted before the cap, both of them: a band ranked by a load that stopped at row
        // twelve would make the busiest project look like the cheapest one.
        if (metric !== null) load += metricValue(node, metric);
        total += 1;
        if (slot >= capacity) continue;
        placements.push({
          node,
          laneIndex: lane,
          col: slot % COLS,
          row: Math.floor(slot / COLS),
        });
      }
    }

    const laneTotals = own.map((cell) => cell.length);
    return { project, laneTotals, load, total, placements };
  });

  // The sorted lane as a column index. A lane the board is not drawing has no column to rank by,
  // so the sort falls away rather than ordering every band by a count that is not there.
  const column = laneSort === null ? undefined : laneIndex.get(laneSort.lane);
  const bandSort = laneSort === null || column === undefined ? null : { column, dir: laneSort.dir };

  return {
    lanes,
    rows,
    bands: bands.sort((a, b) => compareBands(a, b, metric, rank, bandSort)),
    totalNodes,
  };
}

/**
 * Order inside one cell. The worst offenders lead in a shade mode, and admission order settles
 * everything else.
 *
 * Ranked on the RAW metric value rather than on its band, so the ordering does not go flat the
 * moment a threshold puts a whole cell in one shade — the colour answers "how bad", the position
 * still answers "which is worst".
 */
function compareNodes(a: IssueNode, b: IssueNode, metric: Metric | null): number {
  if (metric !== null) {
    const byLoad = metricValue(b, metric) - metricValue(a, metric);
    if (byLoad !== 0) return byLoad;
  }
  return a.id - b.id;
}

/**
 * Order of the bands down the page. Declared order unless the mode has an opinion.
 *
 * A lane sort leads when there is one. The mode's ranking arrives from a hover on the metric
 * rail and can change under the reader's cursor; a lane sort is a press on a heading, and a
 * press must not be overridden by a hover.
 */
function compareBands(
  a: BandLayout,
  b: BandLayout,
  metric: Metric | null,
  rank: Map<number, number>,
  laneSort: { column: number; dir: "asc" | "desc" } | null = null,
): number {
  if (laneSort !== null) {
    const byLane = (b.laneTotals[laneSort.column] ?? 0) - (a.laneTotals[laneSort.column] ?? 0);
    if (byLane !== 0) return laneSort.dir === "asc" ? -byLane : byLane;
  }
  if (metric !== null && b.load !== a.load) return b.load - a.load;
  return (rank.get(a.project.id) ?? 0) - (rank.get(b.project.id) ?? 0);
}
