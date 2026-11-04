import { describe, expect, it } from "vitest";
import { shouldFollowRow } from "./rowLink";

const PLAIN = {
  defaultPrevented: false,
  modified: false,
  onInteractiveElement: false,
  hasTextSelection: false,
  fromElsewhere: false,
};

describe("shouldFollowRow", () => {
  it("follows a plain click on the row's own whitespace", () => {
    expect(shouldFollowRow(PLAIN)).toBe(true);
  });

  it("leaves a click on a nested link or button alone", () => {
    // The GitHub issue link sits inside the row and has to keep going to GitHub.
    expect(shouldFollowRow({ ...PLAIN, onInteractiveElement: true })).toBe(false);
  });

  it("leaves a click from a panel the row opened alone, though React bubbles it to the row", () => {
    expect(shouldFollowRow({ ...PLAIN, fromElsewhere: true })).toBe(false);
  });

  it("leaves a modified click to the browser", () => {
    // Cmd/Ctrl/Shift-click means "open elsewhere"; navigating in place would swallow that.
    expect(shouldFollowRow({ ...PLAIN, modified: true })).toBe(false);
  });

  it("does not navigate on the click that finishes a text selection", () => {
    expect(shouldFollowRow({ ...PLAIN, hasTextSelection: true })).toBe(false);
  });

  it("respects a handler that already claimed the event", () => {
    expect(shouldFollowRow({ ...PLAIN, defaultPrevented: true })).toBe(false);
  });
});
