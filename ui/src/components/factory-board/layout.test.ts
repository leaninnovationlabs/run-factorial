import { describe, expect, it } from "vitest";
import { COLS, layout, MAX_ROWS, MIN_ROWS } from "./layout";
import { type IssueNode, LANES, type Lane, type ProjectBand } from "./model";

const PROJECTS: ProjectBand[] = [
  { id: 1, name: "alpha" },
  { id: 2, name: "beta" },
];

/**
 * A node in a chosen lane.
 *
 * The lane is derived rather than stored, so the helper sets the observable state that produces
 * it — which is also a running check that `laneOf`'s rules are reachable from real ticket fields.
 */
function inLane(id: number, lane: Lane, over: Partial<IssueNode> = {}): IssueNode {
  const base = {
    id,
    key: `acme/repo#${id}`,
    title: null,
    projectId: 1,
    step: "build",
    latestRunStatus: null,
    costUsd: 0,
    minutes: 0,
    tokens: 0,
  };
  switch (lane) {
    case "admitted":
      return { ...base, status: "running", runs: 0, attempt: null, ...over };
    case "running":
      return { ...base, status: "running", runs: 1, attempt: 1, ...over };
    case "retrying":
      return { ...base, status: "running", runs: 3, attempt: 2, ...over };
    case "gate":
      return { ...base, status: "waiting_gate", runs: 2, attempt: 1, ...over };
    case "error":
      return { ...base, status: "blocked", runs: 2, attempt: 1, ...over };
    default:
      return { ...base, status: "done", runs: 4, attempt: null, ...over };
  }
}

/** Node ids in the order they are placed, reading each cell left-to-right then top-to-bottom. */
function cellOrder(nodes: IssueNode[], mode: Parameters<typeof layout>[3]) {
  return layout(nodes, PROJECTS, LANES, mode)
    .bands.flatMap((band) => band.placements)
    .slice()
    .sort((a, b) => a.laneIndex - b.laneIndex || a.row - b.row || a.col - b.col)
    .map((placement) => placement.node.id);
}

describe("layout", () => {
  it("files each node in the lane its state implies, not the step it is on", () => {
    const nodes = [
      inLane(1, "admitted", { step: "plan" }),
      inLane(2, "gate", { step: "plan" }),
      inLane(3, "done", { step: "plan" }),
    ];
    // All three share a step. A step-keyed board would stack them in one column; these belong
    // in three, which is the whole point of the change.
    const placed = layout(nodes, PROJECTS, LANES, "idle").bands[0]?.placements ?? [];
    const laneOfId = new Map(placed.map((p) => [p.node.id, LANES[p.laneIndex]]));
    expect([laneOfId.get(1), laneOfId.get(2), laneOfId.get(3)]).toEqual([
      "admitted",
      "gate",
      "done",
    ]);
  });

  it("keeps nodes in admission order while idle", () => {
    const nodes = [inLane(3, "running"), inLane(1, "running"), inLane(2, "running")];
    expect(cellOrder(nodes, "idle")).toEqual([1, 2, 3]);
  });

  it("breaks ties by id, so a node never shuffles between two modes that rank it the same", () => {
    const nodes = [inLane(9, "running"), inLane(4, "running"), inLane(7, "running")];
    expect(cellOrder(nodes, "shade_cost")).toEqual([4, 7, 9]);
    expect(cellOrder(nodes, "idle")).toEqual([4, 7, 9]);
  });

  it("leaves the bands in declared order when nothing is being selected", () => {
    const nodes = [inLane(1, "running", { projectId: 2 }), inLane(2, "running", { projectId: 1 })];
    const board = layout(nodes, PROJECTS, LANES, "idle");
    expect(board.bands.map((band) => band.project.name)).toEqual(["alpha", "beta"]);
  });

  it("reserves four rows of ten slots however empty the board is", () => {
    expect(MIN_ROWS).toBe(4);
    expect(COLS).toBe(10);
    expect(layout([inLane(1, "running")], PROJECTS, LANES, "idle").rows).toBe(MIN_ROWS);
  });

  it("grows every cell on the board when any single lane overflows", () => {
    // The count is board-wide, not per-band: a band's geometry may not depend on which project
    // is ranked into it, or re-ranking would move squares that are required to stay still.
    const nodes = Array.from({ length: COLS * MIN_ROWS + 1 }, (_, i) => inLane(i + 1, "running"));
    expect(layout(nodes, PROJECTS, LANES, "idle").rows).toBe(MIN_ROWS + 1);
  });

  it("keeps the row count identical across modes, which the crossfade depends on", () => {
    const nodes = Array.from({ length: 61 }, (_, i) =>
      inLane(i + 1, LANES[i % LANES.length] ?? "running"),
    );
    const rows = (["idle", "shade_cost", "shade_time", "shade_retry"] as const).map(
      (mode) => layout(nodes, PROJECTS, LANES, mode).rows,
    );
    expect(new Set(rows).size).toBe(1);
  });

  it("wraps a cell across rows at ten columns", () => {
    const nodes = Array.from({ length: 12 }, (_, i) => inLane(i + 1, "running"));
    const placed = layout(nodes, PROJECTS, LANES, "idle").bands[0]?.placements ?? [];
    expect(placed.find((placement) => placement.node.id === 11)).toMatchObject({ row: 1, col: 0 });
  });

  it("drops a node whose project the board was not asked to draw", () => {
    const board = layout([inLane(1, "running", { projectId: 99 })], PROJECTS, LANES, "idle");
    expect(board.totalNodes).toBe(0);
  });

  describe("the shade modes", () => {
    it("sorts the worst retries to the head of the cell", () => {
      const nodes = [
        inLane(1, "retrying", { attempt: 2 }),
        inLane(2, "retrying", { attempt: 6 }),
        inLane(3, "retrying", { attempt: 3 }),
      ];
      expect(cellOrder(nodes, "shade_retry")).toEqual([2, 3, 1]);
    });

    it("ranks the bands by retries at the current step, never by total runs", () => {
      // `beta`'s ticket has retried more; `alpha`'s has simply advanced further.
      const nodes = [
        inLane(1, "running", { projectId: 1, runs: 9, attempt: 1 }),
        inLane(2, "retrying", { projectId: 2, runs: 3, attempt: 3 }),
      ];
      const board = layout(nodes, PROJECTS, LANES, "shade_retry");
      expect(board.bands.map((band) => band.project.name)).toEqual(["beta", "alpha"]);
      expect(board.bands.map((band) => band.load)).toEqual([2, 0]);
    });

    it("ranks by whichever metric is on, so the four modes can disagree about a project", () => {
      // alpha retries and beta is expensive. Which project belongs at the top is a different
      // answer per mode, and that disagreement is the reason there are four buttons.
      const nodes = [
        inLane(1, "retrying", { projectId: 1, attempt: 4, costUsd: 1 }),
        inLane(2, "running", { projectId: 2, attempt: 1, costUsd: 40 }),
      ];
      const order = (mode: Parameters<typeof layout>[3]) =>
        layout(nodes, PROJECTS, LANES, mode).bands.map((band) => band.project.name);
      expect(order("shade_retry")).toEqual(["alpha", "beta"]);
      expect(order("shade_cost")).toEqual(["beta", "alpha"]);
    });

    it("sums the active metric into `load`, and only the active one", () => {
      const nodes = [inLane(1, "running", { costUsd: 3, minutes: 20, tokens: 5 })];
      const load = (mode: Parameters<typeof layout>[3]) =>
        layout(nodes, PROJECTS, LANES, mode).bands[0]?.load;
      expect(load("shade_cost")).toBe(3);
      expect(load("shade_time")).toBe(20);
      // Outside a shade mode no metric is selected, so nothing reads it and it stays zero.
      expect(load("idle")).toBe(0);
    });

    it("orders on the raw value, not the band, so a flat cell still ranks", () => {
      // Three tickets all past the upper threshold shade identically. The colour answers "how
      // bad"; the position still has to answer "which is worst".
      const nodes = [
        inLane(1, "running", { costUsd: 20 }),
        inLane(2, "running", { costUsd: 90 }),
        inLane(3, "running", { costUsd: 50 }),
      ];
      expect(cellOrder(nodes, "shade_cost")).toEqual([2, 3, 1]);
    });
  });
});

describe("the band depth cap", () => {
  // `rows` is shared by every band, so a project with 400 tickets in one lane would make every
  // band 40 rows deep and push the rest of the factory off the screen.
  const many = Array.from({ length: MAX_ROWS * COLS + 50 }, (_, i) => inLane(i + 1, "done"));

  it("caps how deep a band can get, so one busy project cannot own the board", () => {
    const board = layout(many, PROJECTS, LANES, "idle");
    expect(board.rows).toBe(MAX_ROWS);
    expect(board.bands[0]?.placements.length).toBe(MAX_ROWS * COLS);
  });

  it("still counts what it did not draw, so the gutter cannot lie about the total", () => {
    const board = layout(many, PROJECTS, LANES, "idle");
    expect(board.totalNodes).toBe(many.length);
    expect(board.bands[0]?.total).toBe(many.length);
  });
});
