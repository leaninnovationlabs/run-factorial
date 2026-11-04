/**
 * What a stored preference is allowed to do to the board.
 *
 * `parsePrefs` reads a value nobody validated on the way in — it outlives the version of the app
 * that wrote it, and a browser console can put anything there — so the contract worth testing is
 * that it is TOTAL: every input produces a board that draws, and a value it cannot read costs the
 * reader that one setting rather than the page.
 *
 * The hook around it is three lines of `localStorage` in a try/catch and there is no DOM here to
 * exercise it against; the parsing is the part that can be wrong.
 */

import { describe, expect, it } from "vitest";
import { DEFAULT_DENSITY, DENSITIES } from "./ModeBar";
import { DEFAULT_THRESHOLDS } from "./model";
import { DEFAULT_PREFS, parsePrefs, serializePrefs } from "./prefs";

describe("parsePrefs", () => {
  it("round-trips what the drawer actually sets", () => {
    const prefs = {
      thresholds: { ...DEFAULT_THRESHOLDS, cost: { lower: 7, upper: 42 } },
      density: 0,
    };
    expect(parsePrefs(serializePrefs(prefs))).toEqual(prefs);
  });

  it("falls back to the defaults for anything it cannot read at all", () => {
    for (const raw of [null, undefined, "", "not json", "[]", '"a string"', "7"]) {
      expect(parsePrefs(raw)).toEqual(DEFAULT_PREFS);
    }
  });

  it("keeps the settings it can read when one of them is unusable", () => {
    // Field by field on purpose: a density index that has gone out of range should cost the
    // reader their node size, not the four ramps they tuned.
    const stored = JSON.stringify({
      thresholds: { ...DEFAULT_THRESHOLDS, time: { lower: 5, upper: 9 } },
      density: 99,
    });
    expect(parsePrefs(stored).density).toBe(DEFAULT_DENSITY);
    expect(parsePrefs(stored).thresholds.time).toEqual({ lower: 5, upper: 9 });
  });

  it("restores the metrics a stored blob carries and defaults the ones it does not", () => {
    // Exactly what an entry written before a metric existed looks like.
    const stored = JSON.stringify({ thresholds: { cost: { lower: 2, upper: 3 } } });
    expect(parsePrefs(stored).thresholds.cost).toEqual({ lower: 2, upper: 3 });
    expect(parsePrefs(stored).thresholds.tokens).toEqual(DEFAULT_THRESHOLDS.tokens);
  });

  it("refuses an edge that is not a number these metrics can have", () => {
    // Every one of them is a count or a cost, so a negative edge is not a threshold to honour —
    // and a NaN would put `heatOf` on a ramp that answers false to both of its comparisons.
    for (const pair of [
      { lower: -1, upper: 5 },
      { lower: 1, upper: Number.NaN },
      { lower: "1", upper: "5" },
      null,
    ]) {
      expect(parsePrefs(JSON.stringify({ thresholds: { cost: pair } })).thresholds.cost).toEqual(
        DEFAULT_THRESHOLDS.cost,
      );
    }
  });

  it("accepts every density the switch can actually produce", () => {
    DENSITIES.forEach((_, index) => {
      expect(parsePrefs(JSON.stringify({ density: index })).density).toBe(index);
    });
    expect(parsePrefs(JSON.stringify({ density: -1 })).density).toBe(DEFAULT_DENSITY);
    expect(parsePrefs(JSON.stringify({ density: 1.5 })).density).toBe(DEFAULT_DENSITY);
  });
});
