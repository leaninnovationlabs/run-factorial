/**
 * Files the operator staged for deletion, persisted per project and process like the other drafts
 * (`repoEdits.ts`). Each records the git blob the file was listed at: the server refuses a
 * deletion whose file has since changed on the branch, and the explorer says so first.
 *
 * A file created here and not yet committed is never staged: deleting it only drops its draft.
 */

import type { ConfigDelete } from "../../../api/types";
import { safeGet, safeSet } from "../../../storage";
import { NEW_PROCESS } from "../processDraft";
import { repoLockReason } from "./repoEdits";
import type { ExplorerFile } from "./tree";

/** Path to the blob the deletion started from (`ConfigFile.sha`). */
export type DeletedFiles = Record<string, string>;

const key = (projectId: number, processKey: string | null) =>
  `sf.deletedFiles.${projectId}.${processKey ?? NEW_PROCESS}`;

/** Total, like `parseRepoEdits`: a stored value outlives the app that wrote it. */
export function parseDeletedFiles(raw: string | null): DeletedFiles {
  if (raw === null) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return {};
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return {};
  return Object.fromEntries(
    Object.entries(parsed as Record<string, unknown>).filter(
      (entry): entry is [string, string] => typeof entry[1] === "string",
    ),
  );
}

export function readDeletedFiles(projectId: number, processKey: string | null): DeletedFiles {
  return parseDeletedFiles(safeGet(key(projectId, processKey)));
}

export function writeDeletedFiles(
  projectId: number,
  processKey: string | null,
  files: DeletedFiles,
): void {
  safeSet(key(projectId, processKey), Object.keys(files).length > 0 ? JSON.stringify(files) : null);
}

/** A file that has left the branch: gone from the listing, or pinned and removed at the head. */
function gone(repo: ExplorerFile["repo"]): boolean {
  return !repo || repo.drift === "removed";
}

/** Staged deletions the explorer can no longer vouch for: the file is still on the branch, and
 *  is no longer the one that was staged. */
export function staleDeletes(deleted: DeletedFiles, files: ExplorerFile[]): string[] {
  const byPath = new Map(files.map((file) => [file.path, file.repo]));
  return Object.entries(deleted)
    .filter(([path, base]) => {
      const repo = byPath.get(path);
      return !gone(repo) && (repo?.sha !== base || (repo && repoLockReason(repo) !== undefined));
    })
    .map(([path]) => path);
}

/** Staged deletions whose file has already left the branch. They are done: the server skips
 *  such a path, and holding the commit for one would leave nothing on screen to act on. */
export function settledDeletes(deleted: DeletedFiles, files: ExplorerFile[]): string[] {
  const byPath = new Map(files.map((file) => [file.path, file.repo]));
  return Object.keys(deleted).filter((path) => gone(byPath.get(path)));
}

export function toConfigDeletes(deleted: DeletedFiles): ConfigDelete[] {
  return Object.entries(deleted).map(([path, base]) => ({ path, base_sha: base }));
}

/** The files a Delete may take: one the branch has, at a blob the pin and the head agree on, and
 *  one created here. A proposed file is unticked instead, and a locked one waits for a sync. */
export function deletablePaths(files: ExplorerFile[], created: Iterable<string>): Set<string> {
  const paths = new Set(created);
  for (const file of files) {
    if (file.repo?.sha && repoLockReason(file.repo) === undefined) paths.add(file.path);
  }
  return paths;
}
