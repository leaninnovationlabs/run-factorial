import { describe, expect, it } from "vitest";
import type { ConfigFile } from "../../../api/types";
import {
  deletablePaths,
  parseDeletedFiles,
  settledDeletes,
  staleDeletes,
  toConfigDeletes,
} from "./deletedFiles";

const PATH = ".factorial/agents/plan-agent/system-prompt.md";
const BLOB = "b".repeat(40);

function repo(overrides: Partial<ConfigFile> = {}): ConfigFile {
  return { path: PATH, kind: "prompt", drift: "same", warning: null, sha: BLOB, ...overrides };
}

describe("staged deletions", () => {
  it("calls a delete stale when its file's blob moved or the branch changed the file", () => {
    const deleted = { [PATH]: BLOB };
    expect(staleDeletes(deleted, [{ path: PATH, repo: repo() }])).toEqual([]);
    expect(staleDeletes(deleted, [{ path: PATH, repo: repo({ sha: "c".repeat(40) }) }])).toEqual([
      PATH,
    ]);
    expect(staleDeletes(deleted, [{ path: PATH, repo: repo({ drift: "differs" }) }])).toEqual([
      PATH,
    ]);
  });

  it("calls a delete settled, not stale, once the file has left the branch", () => {
    const deleted = { [PATH]: BLOB };
    // Gone from the listing, or still pinned and removed at the head: either way it is done.
    for (const files of [[], [{ path: PATH, repo: repo({ drift: "removed" }) }]]) {
      expect(staleDeletes(deleted, files)).toEqual([]);
      expect(settledDeletes(deleted, files)).toEqual([PATH]);
    }
    expect(settledDeletes(deleted, [{ path: PATH, repo: repo() }])).toEqual([]);
  });

  it("reads a stored value that is not an object of blobs as no deletions", () => {
    expect(parseDeletedFiles(JSON.stringify({ [PATH]: BLOB, x: 3 }))).toEqual({ [PATH]: BLOB });
    expect(parseDeletedFiles(JSON.stringify([PATH]))).toEqual({});
    expect(parseDeletedFiles("not json")).toEqual({});
    expect(parseDeletedFiles(null)).toEqual({});
  });

  it("sends each deletion with the blob it started from as `base_sha`", () => {
    expect(toConfigDeletes({ [PATH]: BLOB })).toEqual([{ path: PATH, base_sha: BLOB }]);
  });

  it("lets a Delete take an unlocked repo file and a file created here, and nothing else", () => {
    const locked = ".factorial/agents/plan-agent/instructions.md";
    const proposed = ".factorial/agents/plan-agent/agent.yaml";
    const created = ".factorial/notes/runbook.md";
    const files = [
      { path: PATH, repo: repo() },
      { path: locked, repo: repo({ path: locked, drift: "differs" }) },
      {
        path: proposed,
        proposal: {
          path: proposed,
          body: "",
          component: "plan-agent",
          kind: "agent" as const,
          variant: null,
          version: 1,
          facts: {},
          present_in_repo: false,
        },
      },
      { path: created },
    ];
    expect([...deletablePaths(files, [created])].sort()).toEqual([PATH, created].sort());
  });
});
