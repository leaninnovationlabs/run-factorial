import { describe, expect, it } from "vitest";
import type { Focus } from "./focus";
import type { LaneSort } from "./layout";
import { focusParam, parseFocus, parseMetric, parseSort, sortParam } from "./view";

describe("the focus in the address", () => {
  const FOCUSES: Focus[] = [
    { kind: "lane", lane: "gate" },
    { kind: "project", projectId: 12 },
    { kind: "cell", lane: "error", projectId: 7 },
  ];

  it("reads back every kind exactly as it was written", () => {
    for (const focus of FOCUSES) expect(parseFocus(focusParam(focus))).toEqual(focus);
    expect(focusParam(null)).toBeNull();
    expect(parseFocus(null)).toBeNull();
  });

  it("drops what does not parse rather than guessing at the nearest focus", () => {
    // A renamed lane, a project id that is not one, a kind with the wrong number of parts.
    for (const value of [
      "lane:blocked",
      "project:abc",
      "project:0",
      "cell:gate",
      "lane:gate:3",
      "row:1",
    ]) {
      expect(parseFocus(value)).toBeNull();
    }
  });
});

describe("the lane sort in the address", () => {
  it("reads back both directions", () => {
    const sorts: LaneSort[] = [
      { lane: "error", dir: "desc" },
      { lane: "done", dir: "asc" },
    ];
    for (const sort of sorts) expect(parseSort(sortParam(sort))).toEqual(sort);
    expect(sortParam(null)).toBeNull();
  });

  it("drops an unknown lane or direction", () => {
    for (const value of ["error", "error-up", "blocked-desc", ""])
      expect(parseSort(value)).toBeNull();
  });
});

describe("the pinned metric in the address", () => {
  it("names a metric by its key, and nothing else", () => {
    expect(parseMetric("cost")).toBe("cost");
    expect(parseMetric("shade_cost")).toBeNull();
    expect(parseMetric("Cost")).toBeNull();
    expect(parseMetric(null)).toBeNull();
  });
});
