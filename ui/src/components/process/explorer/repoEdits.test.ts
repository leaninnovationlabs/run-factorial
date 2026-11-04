import { describe, expect, it } from "vitest";
import type { ConfigFile } from "../../../api/types";
import {
  parseRepoEdits,
  repoLockReason,
  staleRepoEdits,
  toConfigEdits,
  withEdit,
  withoutEdit,
} from "./repoEdits";

const PATH = ".factorial/agents/plan-agent/system-prompt.md";
const BLOB = "b".repeat(40);

function repo(overrides: Partial<ConfigFile> = {}): ConfigFile {
  return { path: PATH, kind: "prompt", drift: "same", warning: null, sha: BLOB, ...overrides };
}

describe("repo edits", () => {
  it("holds an edit with the blob it started from", () => {
    expect(withEdit({}, PATH, "mine", { original: "theirs", base: BLOB })).toEqual({
      [PATH]: { body: "mine", base: BLOB },
    });
  });

  it("keeps the first base across keystrokes", () => {
    const first = withEdit({}, PATH, "m", { original: "theirs", base: BLOB });
    expect(
      withEdit(first, PATH, "mi", { original: "theirs", base: "c".repeat(40) })[PATH]?.base,
    ).toBe(BLOB);
  });

  it("drops an edit typed back to the repo's copy", () => {
    const held = withEdit({}, PATH, "mine", { original: "theirs", base: BLOB });
    expect(withEdit(held, PATH, "theirs", { original: "theirs", base: BLOB })).toEqual({});
    expect(withoutEdit(held, PATH)).toEqual({});
  });

  it("reads back only what it wrote, and nothing from junk", () => {
    expect(parseRepoEdits(JSON.stringify({ [PATH]: { body: "b", base: BLOB }, x: 3 }))).toEqual({
      [PATH]: { body: "b", base: BLOB },
    });
    expect(parseRepoEdits("not json")).toEqual({});
    expect(parseRepoEdits(null)).toEqual({});
  });

  it("calls an edit stale when the listed blob moved, the file drifted, or it is gone", () => {
    const edits = { [PATH]: { body: "mine", base: BLOB } };
    expect(staleRepoEdits(edits, [{ path: PATH, repo: repo() }])).toEqual([]);
    expect(staleRepoEdits(edits, [{ path: PATH, repo: repo({ sha: "c".repeat(40) }) }])).toEqual([
      PATH,
    ]);
    expect(staleRepoEdits(edits, [{ path: PATH, repo: repo({ drift: "differs" }) }])).toEqual([
      PATH,
    ]);
    expect(staleRepoEdits(edits, [])).toEqual([PATH]);
  });

  it("locks a file the pin and the head disagree on", () => {
    expect(repoLockReason(repo())).toBeUndefined();
    expect(repoLockReason(repo({ drift: "unknown" }))).toBeUndefined();
    expect(repoLockReason(repo({ drift: "differs" }))).toMatch(/^Changed on the factorial branch/);
    expect(repoLockReason(repo({ drift: "added", sha: null }))).toMatch(
      /^Added to the factorial branch/,
    );
  });

  it("sends each edit with its base as `base_sha`", () => {
    expect(toConfigEdits({ [PATH]: { body: "mine", base: BLOB } })).toEqual([
      { path: PATH, body: "mine", base_sha: BLOB },
    ]);
  });
});
