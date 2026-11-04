import { describe, expect, it } from "vitest";
import { laneNameFor, withLaneName } from "./laneName";

describe("withLaneName", () => {
  it("rewrites the top-level name and never a step's", () => {
    const text = "kind: Process\nname: simple\nsteps:\n  - name: plan\n";
    expect(withLaneName(text, "primary")).toBe(
      "kind: Process\nname: primary\nsteps:\n  - name: plan\n",
    );
  });

  it("leaves text without a top-level name alone", () => {
    expect(withLaneName("steps:\n  - name: plan\n", "primary")).toBe("steps:\n  - name: plan\n");
  });
});

describe("laneNameFor", () => {
  it("takes the template's key when it is free, else the first free numbered name", () => {
    expect(laneNameFor("simple", [])).toBe("simple");
    expect(laneNameFor("simple", ["simple", "simple-2"])).toBe("simple-3");
  });
});
