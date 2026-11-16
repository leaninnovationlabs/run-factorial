import { describe, expect, it } from "vitest";
import type { ProcessVersion } from "../../api/types";
import { syncedCommits } from "./syncedCommits";

const REPO = "northwind/harbor-invoices";
const OLD = "a".repeat(40);
const NEW = "b".repeat(40);

function version(overrides: Partial<ProcessVersion>): ProcessVersion {
  return {
    id: 1,
    project_id: 7,
    process_key: "standard",
    source_repo: REPO,
    source_commit_sha: OLD,
    retired_at: null,
    source_ref: "factorial",
    adopted_by: null,
    activated_at: "2026-11-01T10:00:00Z",
    created_at: "2026-11-01T10:00:00Z",
    ...overrides,
  };
}

describe("syncedCommits", () => {
  it("lists one row per commit, newest sync first, with the processes read at it", () => {
    const rows = syncedCommits(
      [
        version({ id: 1, process_key: "standard", source_commit_sha: OLD }),
        version({ id: 2, process_key: "fast", source_commit_sha: OLD }),
        version({
          id: 3,
          process_key: "standard",
          source_commit_sha: NEW,
          activated_at: "2026-11-05T10:00:00Z",
        }),
      ],
      REPO,
    );
    expect(rows.map((row) => [row.sha, row.processes, row.current])).toEqual([
      [NEW, ["standard"], true],
      [OLD, ["fast", "standard"], false],
    ]);
    expect(rows[1]?.versionId).toBe(1);
    expect(rows[0]?.syncedAt).toBe("2026-11-05T10:00:00Z");
  });

  it("takes a commit's time from the sync that last landed on it", () => {
    const [row] = syncedCommits(
      [
        version({ id: 1, process_key: "standard", activated_at: "2026-11-01T10:00:00Z" }),
        version({ id: 2, process_key: "fast", activated_at: "2026-11-09T10:00:00Z" }),
      ],
      REPO,
    );
    expect(row?.syncedAt).toBe("2026-11-09T10:00:00Z");
  });

  it("leaves out a commit read from a repository the project has left", () => {
    const rows = syncedCommits(
      [
        version({ id: 1 }),
        version({ id: 2, source_repo: "northwind/old", source_commit_sha: NEW }),
      ],
      REPO,
    );
    expect(rows.map((row) => row.sha)).toEqual([OLD]);
  });

  it("says who adopted a commit a person vouched for", () => {
    const [row] = syncedCommits([version({ adopted_by: "ada" })], REPO);
    expect(row?.adoptedBy).toBe("ada");
  });

  it("is empty for a project with nothing synced", () => {
    expect(syncedCommits([], REPO)).toEqual([]);
  });
});
