/** The frames' sizes and the reader's saved choices: kept inside the window, and never trusted. */

import { describe, expect, test } from "vitest";
import { clampDock, clampMini, DEFAULT_PREFS, MINI_MIN, parsePrefs, presetOf } from "./frames";

const WINDOW = { width: 1024, height: 768 };

describe("the small window's size", () => {
  test("a size larger than the window is brought inside it, less the margin", () => {
    expect(clampMini({ width: 5000, height: 5000 }, WINDOW)).toEqual({ width: 992, height: 736 });
  });

  test("a size under the smallest is the smallest", () => {
    expect(clampMini({ width: 10, height: 10 }, WINDOW)).toEqual(MINI_MIN);
  });

  test("a size that fits is kept", () => {
    expect(clampMini({ width: 448, height: 576 }, WINDOW)).toEqual({ width: 448, height: 576 });
  });

  test("a preset is known by its size, and a dragged size is none of them", () => {
    expect(presetOf({ width: 384, height: 480 })).toBe("medium");
    expect(presetOf({ width: 400, height: 480 })).toBeNull();
  });

  test("the docked width stays between its limits", () => {
    expect(clampDock(100)).toBe(320);
    expect(clampDock(9000)).toBe(640);
    expect(clampDock(410.4)).toBe(410);
  });
});

describe("the reader's saved choices", () => {
  test("nothing saved is the defaults: a medium window, small text", () => {
    expect(parsePrefs(null)).toEqual(DEFAULT_PREFS);
    expect(DEFAULT_PREFS).toEqual({
      mini: { width: 384, height: 480 },
      dock: 400,
      text: "small",
    });
  });

  test("junk is the defaults", () => {
    expect(parsePrefs("{not json")).toEqual(DEFAULT_PREFS);
    expect(parsePrefs('"a string"')).toEqual(DEFAULT_PREFS);
    expect(parsePrefs("null")).toEqual(DEFAULT_PREFS);
  });

  test("each choice is checked on its own, so one bad value does not lose the rest", () => {
    // `side` is a choice an earlier build kept: it is read past, not kept.
    const stored = JSON.stringify({
      side: "dock",
      mini: { width: "wide", height: 300 },
      dock: 520,
      text: "huge",
    });
    expect(parsePrefs(stored)).toEqual({ ...DEFAULT_PREFS, dock: 520 });
  });

  test("a saved size out of range is brought into range", () => {
    const stored = JSON.stringify({ mini: { width: 20, height: 99999 }, dock: 5 });
    expect(parsePrefs(stored)).toEqual({
      ...DEFAULT_PREFS,
      mini: { width: MINI_MIN.width, height: 99999 },
      dock: 320,
    });
  });
});
