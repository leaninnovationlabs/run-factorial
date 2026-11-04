import { describe, expect, it } from "vitest";
import type { LabelSpec } from "../../api/types";
import { filterLabels, labelCounts } from "./labelFilter";

const label = (name: string, exists: boolean | null): LabelSpec => ({
  name,
  color: "0e8a16",
  description: name,
  exists,
});

/** One of each state, deliberately with the missing one in the middle. */
const LABELS = [
  label("factorial:plan", true),
  label("factorial:build", false),
  label("factorial:test", null),
  label("factorial:done", true),
];

describe("filterLabels", () => {
  it("shows only what the repo lacks under `missing`", () => {
    expect(filterLabels(LABELS, "missing").map((l) => l.name)).toEqual(["factorial:build"]);
  });

  it("shows only what the repo has under `present`", () => {
    expect(filterLabels(LABELS, "present").map((l) => l.name)).toEqual([
      "factorial:plan",
      "factorial:done",
    ]);
  });

  it("puts the missing ones first under `all`, keeping the rest in order", () => {
    expect(filterLabels(LABELS, "all").map((l) => l.name)).toEqual([
      "factorial:build",
      "factorial:plan",
      "factorial:test",
      "factorial:done",
    ]);
  });

  it("leaves an unchecked label out of both verdicts and in `all`", () => {
    // `exists: null` is "the App could not look", which is not evidence either way. Sorting it
    // in with the missing ones would send someone to create a label that may already exist.
    const unchecked = [label("factorial:plan", null)];
    expect(filterLabels(unchecked, "missing")).toEqual([]);
    expect(filterLabels(unchecked, "present")).toEqual([]);
    expect(filterLabels(unchecked, "all")).toHaveLength(1);
  });

  it("does not reorder the source array", () => {
    const before = LABELS.map((l) => l.name);
    filterLabels(LABELS, "all");
    expect(LABELS.map((l) => l.name)).toEqual(before);
  });
});

describe("labelCounts", () => {
  it("counts each state, and `all` is the whole set rather than the other two added up", () => {
    expect(labelCounts(LABELS)).toEqual({ missing: 1, present: 2, all: 4 });
  });
});
