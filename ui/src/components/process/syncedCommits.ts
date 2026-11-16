/**
 * The config history: every commit Factorial has synced from the project's repository, newest
 * sync first. A sync writes one snapshot per process at one commit, so the rows of a commit are
 * folded into one entry. A restore names any one of those rows; the server reads the commit from it.
 */

import type { ProcessVersion } from "../../api/types";

export interface SyncedCommit {
  sha: string;
  /** One of the commit's snapshot rows: what a restore names. */
  versionId: number;
  /** When a sync last landed on the commit. */
  syncedAt: string;
  /** The processes read at the commit, by name. */
  processes: string[];
  adoptedBy: string | null;
  /** The commit the most recent sync landed on: what new tickets run. */
  current: boolean;
}

/** One entry per commit read from `repo`. A commit read from a repository the project has left
 *  is not one a restore could write here. */
export function syncedCommits(versions: ProcessVersion[], repo: string): SyncedCommit[] {
  const bySha = new Map<string, SyncedCommit>();
  for (const version of versions) {
    if (version.source_repo !== repo) continue;
    const held = bySha.get(version.source_commit_sha);
    if (!held) {
      bySha.set(version.source_commit_sha, {
        sha: version.source_commit_sha,
        versionId: version.id,
        syncedAt: version.activated_at,
        processes: [version.process_key],
        adoptedBy: version.adopted_by,
        current: false,
      });
      continue;
    }
    held.processes.push(version.process_key);
    held.versionId = Math.min(held.versionId, version.id);
    if (version.activated_at > held.syncedAt) held.syncedAt = version.activated_at;
    held.adoptedBy ??= version.adopted_by;
  }
  const commits = [...bySha.values()].sort((a, b) => b.syncedAt.localeCompare(a.syncedAt));
  return commits.map((commit, index) => ({
    ...commit,
    processes: [...commit.processes].sort(),
    current: index === 0,
  }));
}
