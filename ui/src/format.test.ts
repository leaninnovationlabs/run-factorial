import { afterEach, describe, expect, it, vi } from "vitest";
import {
  figureSizers,
  formatClock,
  formatCompactCount,
  formatCost,
  formatDuration,
  formatElapsed,
  formatRelative,
  formatRunTime,
  formatTokens,
  metricPieces,
  NO_VALUE,
  shortSha,
} from "./format";

afterEach(() => {
  vi.useRealTimers();
});

describe("formatRunTime", () => {
  it("measures a finished run between its own timestamps", () => {
    expect(formatRunTime("2026-08-19T12:00:00Z", "2026-08-19T12:04:30Z")).toBe("4m 30s");
    expect(formatRunTime("2026-08-19T12:00:00Z", "2026-08-19T13:30:00Z")).toBe("1h 30m");
    expect(formatRunTime("2026-08-19T12:00:00Z", "2026-08-19T12:00:12Z")).toBe("12s");
  });

  it("measures a still-running run against now", () => {
    vi.useFakeTimers().setSystemTime(new Date("2026-08-19T12:09:00Z"));
    expect(formatRunTime("2026-08-19T12:00:00Z", null)).toBe("9m 0s");
  });

  it("is a dash when nothing has started", () => {
    expect(formatRunTime(null, null)).toBe(NO_VALUE);
  });
});

describe("formatDuration", () => {
  it("runs to days for a wait or a lead time, and is formatElapsed under a day", () => {
    expect(formatDuration(42)).toBe("42s");
    expect(formatDuration(185)).toBe("3m 5s");
    expect(formatDuration(22_200)).toBe("6h 10m");
    expect(formatDuration(100_800)).toBe("1d 4h");
    expect(formatDuration(187_200.4)).toBe("2d 4h");
  });
});

describe("formatClock", () => {
  it("shows the second, which is what tells two refreshes a minute apart", () => {
    // 37 is never an hour, and no zone's offset turns :04 into :37, so this holds in any TZ.
    expect(formatClock("2026-09-13T10:04:37Z")).toContain("37");
  });

  it("is a dash when there is no time", () => {
    expect(formatClock(null)).toBe(NO_VALUE);
  });
});

describe("formatElapsed", () => {
  it("is the one place the h/m/s shape is decided", () => {
    expect(formatElapsed(12)).toBe("12s");
    expect(formatElapsed(270)).toBe("4m 30s");
    expect(formatElapsed(5400)).toBe("1h 30m");
  });

  it("never counts backwards, however the clocks disagree", () => {
    expect(formatElapsed(-30)).toBe("0s");
  });
});

describe("formatCost", () => {
  it("shows the cent, which is the unit a bill is read in", () => {
    expect(formatCost("1.9840")).toBe("$1.98");
    expect(formatCost("0")).toBe("$0.00");
  });

  it("says a sub-cent charge is small rather than saying it is nothing", () => {
    expect(formatCost("0.0034")).toBe("<$0.01");
    // Zero is a real figure, not a rounded-away one.
    expect(formatCost("0")).not.toBe("<$0.01");
  });

  it("distinguishes 'no cost recorded' from zero", () => {
    expect(formatCost(null)).toBe(NO_VALUE);
    expect(formatCost("0")).not.toBe(NO_VALUE);
  });

  it("marks an estimate with a tilde and nothing else", () => {
    expect(formatCost("0.9441", true)).toBe("~$0.94");
    expect(formatCost("0.0012", true)).toBe("~<$0.01");
    expect(formatCost(null, true)).toBe(NO_VALUE);
  });
});

describe("formatCompactCount", () => {
  it("abbreviates thousands and millions", () => {
    expect(formatCompactCount(999)).toBe("999");
    expect(formatCompactCount(1000)).toBe("1K");
    expect(formatCompactCount(22104)).toBe("22.1K");
    expect(formatCompactCount(1_200_000)).toBe("1.2M");
  });
});

describe("formatTokens", () => {
  it("shows a partial count rather than hiding the whole row", () => {
    expect(formatTokens(88000, null)).toBe("88K in / 0 out");
  });

  it("abbreviates both sides, so the pair fits a row", () => {
    expect(formatTokens(41880, 9204)).toBe("41.9K in / 9.2K out");
    expect(formatTokens(1_200_000, 800)).toBe("1.2M in / 800 out");
  });

  it("is a dash only when neither side was reported", () => {
    expect(formatTokens(null, null)).toBe(NO_VALUE);
  });
});

describe("formatRelative", () => {
  it("describes how stale a polled row is", () => {
    vi.useFakeTimers().setSystemTime(new Date("2026-08-19T12:00:00Z"));
    expect(formatRelative("2026-08-19T11:57:00Z")).toContain("3 minutes ago");
    expect(formatRelative("2026-08-18T12:00:00Z")).toContain("yesterday");
  });

  it("is a dash for a null timestamp", () => {
    expect(formatRelative(null)).toBe(NO_VALUE);
  });
});

describe("shortSha", () => {
  it("uses the 7-character form every layer uses in errors", () => {
    expect(shortSha("0123456789abcdef")).toBe("0123456");
    expect(shortSha(null)).toBe(NO_VALUE);
  });
});

describe("metricPieces", () => {
  it("pairs a suffix unit with the figure it measures", () => {
    expect(metricPieces("57m 51s")).toEqual([
      { kind: "metric", value: "57", unit: "m", unitFirst: false },
      { kind: "text", text: " " },
      { kind: "metric", value: "51", unit: "s", unitFirst: false },
    ]);
  });

  it("puts a currency mark ahead of its figure, where it is written", () => {
    expect(metricPieces("$0.94")).toEqual([
      { kind: "metric", value: "0.94", unit: "$", unitFirst: true },
    ]);
  });

  it("keeps a magnitude with the figure it scales", () => {
    // `K` says how big the number is, not what it measures — split off, `41.9` would read as a
    // count of something.
    expect(metricPieces("41.9K in / 9.2K out")).toEqual([
      { kind: "metric", value: "41.9K", unit: "in", unitFirst: false },
      { kind: "text", text: " / " },
      { kind: "metric", value: "9.2K", unit: "out", unitFirst: false },
    ]);
  });

  it("keeps everything around a metric as it was written", () => {
    // `formatCost` says `<$0.01` for a real charge too small to itemise; the comparison is part
    // of the claim and cannot be dropped.
    expect(metricPieces("<$0.01")).toEqual([
      { kind: "text", text: "<" },
      { kind: "metric", value: "0.01", unit: "$", unitFirst: true },
    ]);
  });

  it("reads the factory board's own formats too", () => {
    // `model.ts` abbreviates with a lowercase `k` and names the unit in full.
    expect(metricPieces("120k tok")).toEqual([
      { kind: "metric", value: "120k", unit: "tok", unitFirst: false },
    ]);
    expect(metricPieces("$12.50 · 8")).toEqual([
      { kind: "metric", value: "12.50", unit: "$", unitFirst: true },
      { kind: "text", text: " · 8" },
    ]);
  });

  it("leaves a count alone, having no unit to set apart from it", () => {
    // A count's noun is not a unit: nothing is being measured, and boxing `issues` would put the
    // heaviest mark on the board's most ordinary line.
    expect(metricPieces("8 issues")).toEqual([{ kind: "text", text: "8 issues" }]);
    expect(metricPieces("5 of 8")).toEqual([{ kind: "text", text: "5 of 8" }]);
  });

  it("reads retries as a unit, alone among the counts, so its box can be shortened", () => {
    // The exception to the rule above, and it is about width rather than measurement: `retries`
    // is four times the figure it follows and it has to fit a box the size of a `$`. Splitting it
    // off here is what lets `Metric` abbreviate it — see `UNIT_LABELS`.
    expect(metricPieces("3 retries")).toEqual([
      { kind: "metric", value: "3", unit: "retries", unitFirst: false },
    ]);
  });

  it("finds no metric in a placeholder or a bare word", () => {
    expect(metricPieces(NO_VALUE)).toEqual([{ kind: "text", text: NO_VALUE }]);
    // `h`, `m` and `s` are units only where a figure precedes them.
    expect(metricPieces("hms")).toEqual([{ kind: "text", text: "hms" }]);
  });
});

describe("figureSizers", () => {
  it("sizes a single retry with the plural, since both draw one box", () => {
    // `12 retries` over `1 retry`: the same count, written ×, so the boxes must share a width.
    expect(figureSizers(["12 retries", "1 retry"])).toEqual({ retries: ["12", "1"] });
  });

  it("gathers every figure a unit is written with across the set", () => {
    // The point of collecting both: `$8.00` has to be drawn in the room `$10.00` needs, or the
    // two boxes are different sizes and their filled halves step apart.
    expect(figureSizers(["$10.00", "$8.00"])).toEqual({ $: ["10.00", "8.00"] });
  });

  it("hands over the whole set rather than picking a winner, the browser doing the measuring", () => {
    // `1.0M` and `900k` are both four characters and the first is visibly wider — an `M` being
    // wider than a digit where a `k` is narrower. Anything that chose between them here would
    // choose by length and size the box a pixel short.
    expect(figureSizers(["1.0M tok", "900k tok"])).toEqual({ tok: ["1.0M", "900k"] });
  });

  it("keys by unit, not by position, so an hour is never measured against a minute", () => {
    // `1h 35m` over `50m` has an hour on one line and not the other. Sizing the first box of each
    // against the other would put the hour's room on a figure counting minutes.
    expect(figureSizers(["1h 35m", "50m"])).toEqual({ h: ["1"], m: ["35", "50"] });
  });

  it("lists a repeated figure once, there being nothing to size against itself", () => {
    expect(figureSizers(["$8.00", "$8.00"])).toEqual({ $: ["8.00"] });
  });

  it("ignores whatever is written around the figures", () => {
    expect(figureSizers(["<$0.01", "$12.00"])).toEqual({ $: ["0.01", "12.00"] });
    expect(figureSizers([NO_VALUE, "8 issues"])).toEqual({});
  });

  it("is empty for an empty set, which is a set nothing has to line up in", () => {
    expect(figureSizers([])).toEqual({});
  });
});
