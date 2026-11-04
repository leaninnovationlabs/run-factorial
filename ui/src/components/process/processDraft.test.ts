import { describe, expect, it } from "vitest";
import { key, NEW_PROCESS, resolveSeed } from "./processDraft";

describe("key", () => {
  it("separates two lanes of one project", () => {
    // A key holding the project alone restored one lane's draft into another — silently, and
    // into the editor that commits to factorial.
    expect(key(9, "standard")).not.toEqual(key(9, "hotfix"));
    expect(key(9, "standard")).not.toEqual(key(10, "standard"));
  });

  it("gives an unsaved new process an identity of its own", () => {
    // There is no lane key yet, so every unsaved new process shares one — but it is still not
    // the key of any lane that exists.
    expect(key(9, null)).toContain(NEW_PROCESS);
    expect(key(9, null)).not.toEqual(key(9, "standard"));
  });
});

const CURRENT = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0";
const OLDER = "9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c";

describe("resolveSeed", () => {
  it("opens on the baseline when no draft survives", () => {
    expect(resolveSeed("yaml: 1", CURRENT, null)).toEqual({
      text: "yaml: 1",
      seededSha: CURRENT,
    });
  });

  it("a surviving draft beats the baseline and keeps ITS commit", () => {
    expect(resolveSeed("yaml: new", CURRENT, { sha: OLDER, text: "yaml: edited" })).toEqual({
      text: "yaml: edited",
      // So the "a newer commit is current" warning fires instead of mislabeling the draft as a
      // copy of a commit it was never taken from.
      seededSha: OLDER,
    });
  });

  it("a draft from before commits identified a snapshot wears the current one", () => {
    expect(resolveSeed("yaml: new", CURRENT, { sha: null, text: "edited" })).toEqual({
      text: "edited",
      seededSha: CURRENT,
    });
  });

  it("a draft identical to the baseline is stale bookkeeping, not a draft", () => {
    expect(resolveSeed("yaml: 1", CURRENT, { sha: OLDER, text: "yaml: 1" })).toEqual({
      text: "yaml: 1",
      seededSha: CURRENT,
    });
  });
});
