import { describe, expect, it } from "vitest";
import { clampWidth, DEFAULT_WIDTH, MIN_WIDTH, parseWidth } from "./paneWidth";

describe("the file panel's width", () => {
  it("keeps a width between its minimum and two fifths of the editor", () => {
    expect(clampWidth(300, 1000)).toBe(300);
    expect(clampWidth(50, 1000)).toBe(MIN_WIDTH);
    expect(clampWidth(900, 1000)).toBe(400);
  });

  it("lets two fifths win in an editor too narrow for the minimum", () => {
    expect(clampWidth(300, 400)).toBe(160);
  });

  it("leaves a width alone while the editor has not been measured", () => {
    expect(clampWidth(300, 0)).toBe(300);
  });

  it("reads a stored width, and the default from anything else", () => {
    expect(parseWidth("320")).toBe(320);
    for (const junk of [null, "", "wide", "-4", "NaN"])
      expect(parseWidth(junk)).toBe(DEFAULT_WIDTH);
  });
});
