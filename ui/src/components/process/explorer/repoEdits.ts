/**
 * Edits to files the `factorial` branch already has, persisted per project and process like the
 * bundle draft (`bundleDraft.ts`). Each records the git blob it started from: the server refuses
 * an edit whose file has since changed on the branch, and the explorer says so first.
 */

import type { ConfigEdit, ConfigFile } from "../../../api/types";
import { safeGet, safeSet } from "../../../storage";
import { NEW_PROCESS } from "../processDraft";
import type { ExplorerFile } from "./tree";

export interface RepoEdit {
  body: string;
  /** The blob the edit started from (`ConfigFile.sha`). */
  base: string;
}

export type RepoEdits = Record<string, RepoEdit>;

export const key = (projectId: number, processKey: string | null) =>
  `sf.repoEdits.${projectId}.${processKey ?? NEW_PROCESS}`;

/** Total, like `parseDraft`: a stored value outlives the app that wrote it. */
export function parseRepoEdits(raw: string | null): RepoEdits {
  if (raw === null) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return {};
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return {};
  const edits: RepoEdits = {};
  for (const [path, value] of Object.entries(parsed as Record<string, unknown>)) {
    if (typeof value !== "object" || value === null) continue;
    const { body, base } = value as { body?: unknown; base?: unknown };
    if (typeof body === "string" && typeof base === "string") edits[path] = { body, base };
  }
  return edits;
}

export function readRepoEdits(projectId: number, processKey: string | null): RepoEdits {
  return parseRepoEdits(safeGet(key(projectId, processKey)));
}

export function writeRepoEdits(
  projectId: number,
  processKey: string | null,
  edits: RepoEdits,
): void {
  safeSet(key(projectId, processKey), Object.keys(edits).length > 0 ? JSON.stringify(edits) : null);
}

/** `edits` with `path` holding `body`, or without it once `body` is the repo's copy again. The
 *  base is the one the edit started from, not the file's blob now. */
export function withEdit(
  edits: RepoEdits,
  path: string,
  body: string,
  file: { original: string; base: string },
): RepoEdits {
  if (body === file.original) return withoutEdit(edits, path);
  return { ...edits, [path]: { body, base: edits[path]?.base ?? file.base } };
}

export function withoutEdit(edits: RepoEdits, path: string): RepoEdits {
  const next = { ...edits };
  delete next[path];
  return next;
}

/** Why a repo file cannot be edited: the pin and the branch head disagree on it, so the blob an
 *  edit would start from is not the one the commit builds on. */
export function repoLockReason(repo: ConfigFile): string | undefined {
  if (!repo.sha || repo.drift === "added")
    return "Added to the factorial branch since this version was synced. Sync config to edit it.";
  if (repo.drift === "differs")
    return "Changed on the factorial branch since this version was synced. Sync config to edit it.";
  if (repo.drift === "removed")
    return "Removed from the factorial branch since this version was synced.";
  return undefined;
}

/** Held edits the explorer can no longer vouch for. */
export function staleRepoEdits(edits: RepoEdits, files: ExplorerFile[]): string[] {
  const byPath = new Map(files.map((file) => [file.path, file.repo]));
  return Object.entries(edits)
    .filter(([path, edit]) => {
      const repo = byPath.get(path);
      return !repo || repo.sha !== edit.base || repoLockReason(repo) !== undefined;
    })
    .map(([path]) => path);
}

export function toConfigEdits(edits: RepoEdits): ConfigEdit[] {
  return Object.entries(edits).map(([path, edit]) => ({
    path,
    body: edit.body,
    base_sha: edit.base,
  }));
}
