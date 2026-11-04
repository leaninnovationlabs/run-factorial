import { describe, expect, it } from "vitest";
import {
  cornersAt,
  focusedNodes,
  geometryOf,
  gutterFocused,
  inFocus,
  laneHeadFocused,
  samePlace,
} from "./focus";
import { type IssueNode, LANES, type Lane } from "./model";

const BANDS = [1, 2, 3];

function node(id: number, projectId: number, lane: Lane): IssueNode {
  const base = {
    id,
    key: `a#${id}`,
    title: `Issue ${id}`,
    projectId,
    step: "build",
    latestRunStatus: null,
    costUsd: 0,
    minutes: 0,
    tokens: 0,
  };
  switch (lane) {
    case "gate":
      return { ...base, status: "waiting_gate", runs: 1, attempt: 1 };
    case "error":
      return { ...base, status: "blocked", runs: 1, attempt: 1 };
    case "done":
      return { ...base, status: "done", runs: 3, attempt: null };
    default:
      return { ...base, status: "running", runs: 1, attempt: 1 };
  }
}

describe("inFocus", () => {
  it("keeps everything when nothing is focused", () => {
    expect(inFocus(null, node(1, 1, "gate"))).toBe(true);
  });

  it("keeps a whole column for a lane focus", () => {
    const focus = { kind: "lane", lane: "gate" } as const;
    expect(inFocus(focus, node(1, 1, "gate"))).toBe(true);
    expect(inFocus(focus, node(2, 9, "gate"))).toBe(true);
    expect(inFocus(focus, node(3, 1, "error"))).toBe(false);
  });

  it("keeps a whole row for a project focus", () => {
    const focus = { kind: "project", projectId: 2 } as const;
    expect(inFocus(focus, node(1, 2, "gate"))).toBe(true);
    expect(inFocus(focus, node(2, 2, "done"))).toBe(true);
    expect(inFocus(focus, node(3, 1, "gate"))).toBe(false);
  });

  it("keeps only the intersection for a cell focus", () => {
    const focus = { kind: "cell", lane: "gate", projectId: 2 } as const;
    expect(inFocus(focus, node(1, 2, "gate"))).toBe(true);
    expect(inFocus(focus, node(2, 2, "error"))).toBe(false);
    expect(inFocus(focus, node(3, 1, "gate"))).toBe(false);
  });
});

describe("geometryOf", () => {
  it("leaves the unconstrained axis at -1", () => {
    expect(geometryOf({ kind: "lane", lane: "error" }, LANES, BANDS)).toEqual({
      kind: "lane",
      band: -1,
      lane: LANES.indexOf("error"),
    });
    expect(geometryOf({ kind: "project", projectId: 3 }, LANES, BANDS)).toEqual({
      kind: "project",
      band: 2,
      lane: -1,
    });
  });

  it("resolves a project to the row it currently occupies, not its declared order", () => {
    // The bands re-rank, so the rail has to be drawn where the project actually sits now.
    expect(geometryOf({ kind: "project", projectId: 1 }, LANES, [3, 2, 1])?.band).toBe(2);
  });

  it("lights nothing when the focus names something the board is not drawing", () => {
    expect(geometryOf({ kind: "project", projectId: 99 }, LANES, BANDS)).toBeNull();
    expect(geometryOf({ kind: "cell", lane: "gate", projectId: 99 }, LANES, BANDS)).toBeNull();
  });
});

describe("cornersAt", () => {
  const BANDS_N = BANDS.length;
  const LANES_N = LANES.length;
  const at = (focus: Parameters<typeof geometryOf>[0], band: number, lane: number) =>
    cornersAt(geometryOf(focus, LANES, BANDS), band, lane, BANDS_N, LANES_N);
  const GATE = LANES.indexOf("gate");
  const ERROR = LANES.indexOf("error");

  it("splits a column's rectangle into top corners and bottom corners", () => {
    const focus = { kind: "lane", lane: "gate" } as const;
    expect(at(focus, 0, GATE)).toEqual([true, true, false, false]);
    expect(at(focus, BANDS_N - 1, GATE)).toEqual([false, false, true, true]);
    // Everything between carries nothing; the eye closes the rectangle itself.
    expect(at(focus, 1, GATE)).toBeNull();
    expect(at(focus, 0, ERROR)).toBeNull();
  });

  it("splits a row's rectangle into left corners and right corners", () => {
    const focus = { kind: "project", projectId: 2 } as const;
    expect(at(focus, 1, 0)).toEqual([true, false, true, false]);
    expect(at(focus, 1, LANES_N - 1)).toEqual([false, true, false, true]);
    expect(at(focus, 1, GATE)).toBeNull();
    expect(at(focus, 0, 0)).toBeNull();
  });

  it("gives a cell focus all four corners of its own group", () => {
    const focus = { kind: "cell", lane: "gate", projectId: 2 } as const;
    expect(at(focus, 1, GATE)).toEqual([true, true, true, true]);
    expect(at(focus, 0, GATE)).toBeNull();
    expect(at(focus, 1, 0)).toBeNull();
  });

  it("gives one group all four corners when it is both ends of the span", () => {
    // A single-band board makes band 0 both the top and the bottom of its column.
    const solo = geometryOf({ kind: "lane", lane: "gate" }, LANES, [1]);
    expect(cornersAt(solo, 0, GATE, 1, LANES_N)).toEqual([true, true, true, true]);
  });

  it("always emits four corners in total across the focused set", () => {
    const focus = { kind: "lane", lane: "gate" } as const;
    const pips = Array.from({ length: BANDS_N }, (_, band) => at(focus, band, GATE))
      .filter((corners) => corners !== null)
      .flatMap((corners) => [...corners])
      .filter(Boolean);
    expect(pips).toHaveLength(4);
  });
});

describe("heading highlights", () => {
  it("lights the lane heading for a lane focus and a cell focus, not a project focus", () => {
    const lane = LANES.indexOf("gate");
    expect(laneHeadFocused(geometryOf({ kind: "lane", lane: "gate" }, LANES, BANDS), lane)).toBe(
      true,
    );
    expect(
      laneHeadFocused(geometryOf({ kind: "cell", lane: "gate", projectId: 2 }, LANES, BANDS), lane),
    ).toBe(true);
    expect(laneHeadFocused(geometryOf({ kind: "project", projectId: 2 }, LANES, BANDS), lane)).toBe(
      false,
    );
  });

  it("lights the gutter for a project focus and a cell focus, not a lane focus", () => {
    expect(gutterFocused(geometryOf({ kind: "project", projectId: 2 }, LANES, BANDS), 1)).toBe(
      true,
    );
    expect(
      gutterFocused(geometryOf({ kind: "cell", lane: "gate", projectId: 2 }, LANES, BANDS), 1),
    ).toBe(true);
    expect(gutterFocused(geometryOf({ kind: "lane", lane: "gate" }, LANES, BANDS), 1)).toBe(false);
  });
});

describe("focusedNodes", () => {
  it("selects every node when nothing is focused", () => {
    const nodes = [node(1, 1, "gate"), node(2, 2, "error")];
    expect(focusedNodes(null, nodes)).toEqual(nodes);
  });

  it("selects the intersection for a cell focus", () => {
    const nodes = [node(1, 1, "gate"), node(2, 2, "gate"), node(3, 2, "error")];
    const picked = focusedNodes({ kind: "cell", lane: "gate", projectId: 2 }, nodes);
    expect(picked.map((n) => n.id)).toEqual([2]);
  });
});

describe("samePlace", () => {
  it("recognises a second click on the same target, which turns the focus off", () => {
    expect(samePlace({ kind: "lane", lane: "gate" }, { kind: "lane", lane: "gate" })).toBe(true);
    expect(samePlace({ kind: "lane", lane: "gate" }, { kind: "lane", lane: "error" })).toBe(false);
  });

  it("does not confuse a cell with the lane or project it sits on", () => {
    const cell = { kind: "cell", lane: "gate", projectId: 2 } as const;
    expect(samePlace(cell, { kind: "lane", lane: "gate" })).toBe(false);
    expect(samePlace(cell, { kind: "project", projectId: 2 })).toBe(false);
    expect(samePlace(cell, cell)).toBe(true);
  });
});
