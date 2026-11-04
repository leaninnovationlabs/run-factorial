import { describe, expect, it } from "vitest";
import type { Focus } from "./focus";
import { COLS, layout } from "./layout";
import { DEFAULT_THRESHOLDS, type IssueNode, LANES, type Lane, type ProjectBand } from "./model";
import { changedSlots, slotsOf } from "./slots";

const PROJECTS: ProjectBand[] = [
  { id: 1, name: "alpha" },
  { id: 2, name: "beta" },
];

function inLane(id: number, lane: Lane, over: Partial<IssueNode> = {}): IssueNode {
  const base = {
    id,
    key: `a#${id}`,
    title: `Issue ${id}`,
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
    case "retrying":
      return { ...base, status: "running", runs: 3, attempt: 2, ...over };
    case "gate":
      return { ...base, status: "waiting_gate", runs: 2, attempt: 1, ...over };
    case "error":
      return { ...base, status: "blocked", runs: 2, attempt: 1, ...over };
    case "done":
      return { ...base, status: "done", runs: 4, attempt: null, ...over };
    default:
      return { ...base, status: "running", runs: 1, attempt: 1, ...over };
  }
}

const slots = (
  nodes: IssueNode[],
  mode: Parameters<typeof layout>[3],
  focus: Focus | null = null,
) => slotsOf(layout(nodes, PROJECTS, LANES, mode), mode, focus);

/** The first slot of a given lane in the first band. */
const head = (all: ReturnType<typeof slots>, lane: Lane, rows: number) =>
  all[LANES.indexOf(lane) * rows * COLS];

describe("slotsOf", () => {
  it("covers every position on the board, occupied or not", () => {
    const board = layout([inLane(1, "running")], PROJECTS, LANES, "idle");
    expect(slotsOf(board, "idle")).toHaveLength(PROJECTS.length * LANES.length * board.rows * COLS);
  });

  it("marks the positions no ticket reaches as empty", () => {
    const all = slots([inLane(1, "admitted")], "idle");
    expect(all[0]?.appearance).toBe("admitted");
    expect(all[1]?.appearance).toBe("empty");
    expect(all.filter((slot) => slot.appearance === "empty")).toHaveLength(all.length - 1);
  });

  it("paints a node with its lane, so the colour and the column always agree", () => {
    const board = layout([inLane(1, "gate"), inLane(2, "error")], PROJECTS, LANES, "idle");
    const all = slotsOf(board, "idle");
    expect(head(all, "gate", board.rows)?.appearance).toBe("gate");
    expect(head(all, "error", board.rows)?.appearance).toBe("error");
  });

  it("shades by retries at the current step in the retry shade mode", () => {
    const board = layout(
      [inLane(1, "retrying", { attempt: 3 }), inLane(2, "running", { runs: 8 })],
      PROJECTS,
      LANES,
      "shade_retry",
    );
    const all = slotsOf(board, "shade_retry");
    expect(head(all, "retrying", board.rows)?.appearance).toBe("heat-2");
    // Eight runs but a first attempt: no retries, and it must not be shaded as one.
    expect(head(all, "running", board.rows)?.appearance).toBe("heat-0");
  });

  it("indexes the same screen position identically whatever the mode", () => {
    const nodes = [inLane(1, "error"), inLane(2, "running"), inLane(3, "gate")];
    const idle = slots(nodes, "idle");
    const shaded = slots(nodes, "shade_cost");
    expect(shaded).toHaveLength(idle.length);
    for (const [i, slot] of idle.entries()) {
      expect([slot.bandIndex, slot.laneIndex, slot.row, slot.col]).toEqual([
        shaded[i]?.bandIndex,
        shaded[i]?.laneIndex,
        shaded[i]?.row,
        shaded[i]?.col,
      ]);
    }
  });

  describe("focus", () => {
    it("mutes every lane but the focused one", () => {
      const board = layout([inLane(1, "gate"), inLane(2, "error")], PROJECTS, LANES, "idle");
      const all = slotsOf(board, "idle", { kind: "lane", lane: "gate" });
      expect(head(all, "gate", board.rows)?.appearance).toBe("gate");
      expect(head(all, "error", board.rows)?.appearance).toBe("muted");
    });

    it("keeps a whole project row for a project focus", () => {
      const nodes = [inLane(1, "gate", { projectId: 1 }), inLane(2, "error", { projectId: 1 })];
      const board = layout(nodes, PROJECTS, LANES, "idle");
      const all = slotsOf(board, "idle", { kind: "project", projectId: 1 });
      expect(head(all, "gate", board.rows)?.appearance).toBe("gate");
      expect(head(all, "error", board.rows)?.appearance).toBe("error");
    });

    it("keeps only the intersection for a cell focus", () => {
      const nodes = [inLane(1, "gate", { projectId: 1 }), inLane(2, "error", { projectId: 1 })];
      const board = layout(nodes, PROJECTS, LANES, "idle");
      const all = slotsOf(board, "idle", { kind: "cell", lane: "gate", projectId: 1 });
      expect(head(all, "gate", board.rows)?.appearance).toBe("gate");
      expect(head(all, "error", board.rows)?.appearance).toBe("muted");
    });

    it("leaves the empty slots alone, so the lattice does not move under the focus", () => {
      const board = layout([inLane(1, "gate")], PROJECTS, LANES, "idle");
      const focused = slotsOf(board, "idle", { kind: "lane", lane: "error" });
      expect(focused.filter((slot) => slot.appearance === "empty")).toHaveLength(
        focused.length - 1,
      );
    });
  });
});

describe("changedSlots", () => {
  it("finds nothing when the board is unchanged", () => {
    const nodes = [inLane(1, "running"), inLane(2, "running")];
    expect(changedSlots(slots(nodes, "idle"), slots(nodes, "idle")).size).toBe(0);
  });

  it("ignores two identical-looking nodes trading places", () => {
    const nodes = [inLane(1, "running", { costUsd: 0.2 }), inLane(2, "running", { costUsd: 0.5 })];
    expect(changedSlots(slots(nodes, "shade_time"), slots(nodes, "shade_cost")).size).toBe(0);
  });

  it("finds exactly the positions a focus mutes", () => {
    const nodes = [inLane(1, "gate"), inLane(2, "error"), inLane(3, "running")];
    // Focusing `gate` mutes the two nodes in other lanes and leaves the gate node alone.
    const changed = changedSlots(
      slots(nodes, "idle"),
      slots(nodes, "idle", { kind: "lane", lane: "gate" }),
    );
    expect(changed.size).toBe(2);
  });

  it("treats a resized board as wholly changed rather than mis-aligning it", () => {
    expect(changedSlots(slots([inLane(1, "running")], "idle"), []).size).toBe(0);
    expect(changedSlots([], slots([inLane(1, "running")], "idle")).size).toBeGreaterThan(0);
  });
});

describe("thresholds", () => {
  it("re-shades without relaying out, so moving a ramp crossfades like any other change", () => {
    // The board's contract is that nothing moves. A threshold reaches `appearanceOf` and nothing
    // else, so the same ticket must stay in the same screen position and only change colour.
    const nodes = [inLane(1, "running", { costUsd: 4 })];
    const board = layout(nodes, PROJECTS, LANES, "shade_cost");
    const loose = slotsOf(board, "shade_cost", null, {
      ...DEFAULT_THRESHOLDS,
      cost: { lower: 1, upper: 5 },
    });
    const strict = slotsOf(board, "shade_cost", null, {
      ...DEFAULT_THRESHOLDS,
      cost: { lower: 1, upper: 3 },
    });
    expect(head(loose, "running", board.rows)?.appearance).toBe("heat-2");
    expect(head(strict, "running", board.rows)?.appearance).toBe("heat-3");
    expect(changedSlots(loose, strict).size).toBe(1);
  });

  it("shades every metric off the same ramp, so one legend explains all four", () => {
    const nodes = [inLane(1, "running", { minutes: 600, tokens: 10, costUsd: 0 })];
    const board = layout(nodes, PROJECTS, LANES, "shade_time");
    expect(head(slotsOf(board, "shade_time"), "running", board.rows)?.appearance).toBe("heat-3");
    expect(head(slotsOf(board, "shade_tokens"), "running", board.rows)?.appearance).toBe("heat-0");
  });
});
