import { describe, expect, it } from "vitest";
import { atFoot, FOOT_SLACK_PX } from "./follow";

const view = (scrollTop: number, scrollHeight = 1000, clientHeight = 400) => ({
  scrollTop,
  scrollHeight,
  clientHeight,
});

describe("atFoot", () => {
  it("is true at the foot and false where a reader has scrolled back", () => {
    expect(atFoot(view(600))).toBe(true);
    expect(atFoot(view(0))).toBe(false);
    expect(atFoot(view(200))).toBe(false);
  });

  it("allows the slack, so a sub-pixel scroll height does not read as leaving", () => {
    // A zoomed page or a fractional line height routinely lands a few pixels short of the foot.
    // Without the slack the log stops following there, for no reason the reader can see.
    expect(atFoot(view(600 - FOOT_SLACK_PX))).toBe(true);
    expect(atFoot(view(600 - FOOT_SLACK_PX - 1))).toBe(false);
  });

  it("is true for a log too short to scroll", () => {
    // Nothing has arrived yet, or the run said very little. The reader is at the foot by default,
    // and the first turn to arrive must not have to fight for the viewport.
    expect(atFoot(view(0, 120, 400))).toBe(true);
  });
});
