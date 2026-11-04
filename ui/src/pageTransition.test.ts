/**
 * The rule that decides whether a location change gets an exit: it is the one part of the page
 * swap that is a decision rather than a stylesheet, and getting it wrong is invisible in review —
 * a page that never fades and a page held back for nothing look the same in the markup.
 */

import { describe, expect, it } from "vitest";
import { transitionStep } from "./pageTransition";

describe("transitionStep", () => {
  it("fades the page out before adopting a different one", () => {
    expect(transitionStep("/overview", "/projects", false)).toBe("fade");
  });

  it("holds still when only the query or hash moved", () => {
    expect(transitionStep("/overview", "/overview", false)).toBe("hold");
  });

  it("swaps without motion under prefers-reduced-motion", () => {
    // Not `fade` with the animation disabled downstream: that would keep the delay and drop the
    // only thing it was buying.
    expect(transitionStep("/overview", "/projects", true)).toBe("swap");
  });

  it("does not spend an exit on the bare redirect route", () => {
    expect(transitionStep("/", "/overview", false)).toBe("swap");
  });
});
