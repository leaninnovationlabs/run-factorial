import { describe, expect, it } from "vitest";
import { runtimeInText, withRuntimeLine } from "./runtimeLine";

const BASE = "# header\nspecVersion: factorial/v1\nkind: Process\nname: p\nversion: 2\nsteps: []\n";

describe("runtimeInText", () => {
  it("is undefined for a blank editor: nothing has been said yet", () => {
    expect(runtimeInText("")).toBeUndefined();
    expect(runtimeInText("  \n")).toBeUndefined();
  });

  it("is null for text that names no runtime", () => {
    expect(runtimeInText(BASE)).toBeNull();
  });

  it("reads the top-level key, quoted or commented, and never an indented one", () => {
    expect(runtimeInText(`${BASE}runtime: claude-api\n`)).toBe("claude-api");
    expect(runtimeInText(`${BASE}runtime: "claude-api" # prod\n`)).toBe("claude-api");
    expect(runtimeInText(`${BASE}steps:\n  - runtime: claude-api\n`)).toBeNull();
  });
});

describe("withRuntimeLine", () => {
  it("inserts the line after version: when the text has none", () => {
    const out = withRuntimeLine(BASE, "claude-api");
    expect(out).toContain("version: 2\nruntime: claude-api\nsteps: []");
    expect(runtimeInText(out)).toBe("claude-api");
  });

  it("replaces an existing line in place", () => {
    const out = withRuntimeLine(`${BASE}runtime: codex-prod\n`, "claude-api");
    expect(out.match(/^runtime:/gm)).toHaveLength(1);
    expect(runtimeInText(out)).toBe("claude-api");
  });

  it("removes the line for null and leaves text without one alone", () => {
    expect(withRuntimeLine(`${BASE}runtime: codex-prod\n`, null)).toBe(BASE);
    expect(withRuntimeLine(BASE, null)).toBe(BASE);
  });

  it("inserts the line under a commented-out example, which it leaves in place", () => {
    const text = "name: p\n# runtime: claude-api\nsteps: []\n";
    const out = withRuntimeLine(text, "codex-prod");
    expect(out).toBe("name: p\n# runtime: claude-api\nruntime: codex-prod\nsteps: []\n");
    expect(runtimeInText(text)).toBeNull();
    expect(withRuntimeLine(out, null)).toBe(text);
  });

  it("appends when there is no version: line to anchor on", () => {
    expect(withRuntimeLine("name: p\n", "claude-api")).toBe("name: p\nruntime: claude-api\n");
  });
});
