/**
 * The bundle explorer's uncommitted draft: which proposed files the operator has hand-edited,
 * persisted per project and process so leaving the page costs nothing. Mirrors
 * `../processDraft.ts` — same key shape (the process is in it for the same reason: a project runs
 * several lanes, and one key for all of them restores one lane's edits into another), same
 * storage helpers, same idea of recording what a draft was written against, here per file instead
 * of for the one process text.
 *
 * `parseDraft` / `serializeDraft` are pure and total: a stored draft outlives the version of the
 * app that wrote it, and a browser console can put anything under the key, so a shape that
 * cannot be read must cost the reader that one file rather than the page. `readBundleDraft` /
 * `writeBundleDraft` are one line each over `storage.ts`, which carries the failure handling and
 * its tests.
 */

import type { FileProposal } from "../../../api/types";
import { safeGet, safeSet } from "../../../storage";
import { NEW_PROCESS } from "../processDraft";

export interface DraftFile {
  body: string;
  facts: Record<string, string>;
}

export const key = (projectId: number, processKey: string | null) =>
  `sf.bundleDraft.${projectId}.${processKey ?? NEW_PROCESS}`;

/** Where a draft written before the key grew a process lives — see `processDraft.ts`. */
const legacyKey = (projectId: number) => `sf.bundleDraft.${projectId}`;

export function parseDraft(raw: string | null): Record<string, DraftFile> | null {
  if (raw === null) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return null;
  const draft: Record<string, DraftFile> = {};
  for (const [path, value] of Object.entries(parsed as Record<string, unknown>)) {
    const file = parseDraftFile(value);
    // A path whose stored value does not even look like a DraftFile is dropped rather than
    // failing the whole draft — the other files the operator edited still deserve to come back.
    if (file) draft[path] = file;
  }
  return draft;
}

function parseDraftFile(value: unknown): DraftFile | null {
  if (typeof value !== "object" || value === null) return null;
  const { body, facts } = value as { body?: unknown; facts?: unknown };
  if (typeof body !== "string") return null;
  return { body, facts: parseFacts(facts) };
}

function parseFacts(value: unknown): Record<string, string> {
  if (typeof value !== "object" || value === null) return {};
  const facts: Record<string, string> = {};
  for (const [marker, markerValue] of Object.entries(value as Record<string, unknown>)) {
    if (typeof markerValue === "string") facts[marker] = markerValue;
  }
  return facts;
}

export function serializeDraft(draft: Record<string, DraftFile>): string {
  return JSON.stringify(draft);
}

export function readBundleDraft(
  projectId: number,
  processKey: string | null,
): Record<string, DraftFile> | null {
  return parseDraft(safeGet(key(projectId, processKey)) ?? safeGet(legacyKey(projectId)));
}

export function writeBundleDraft(
  projectId: number,
  processKey: string | null,
  draft: Record<string, DraftFile>,
): void {
  safeSet(key(projectId, processKey), serializeDraft(draft));
}

/**
 * Carries the operator's edits across a config change (a new `process.yaml`, a re-run preview).
 *
 * An untouched file is silently refreshed to the new proposal — there was nothing to lose. An
 * EDITED file is kept, never discarded or silently trusted: a hand-edited prompt still naming
 * `spec/` after the config moved the branch prefix to `release/` still parses green and still
 * satisfies no postcondition, and the orchestrator's loader cannot see the problem because the
 * file is prose to it, not config. So the edit survives, and is flagged stale only when a fact
 * that FILE actually used has since changed — comparing against every fact in the new proposal
 * would flag `instructions.md` for a marker only `system-prompt.md` ever read, crying wolf on an
 * edit nothing downstream of it can see.
 */
export function reconcile(
  draft: Record<string, DraftFile>,
  files: FileProposal[],
  pristine: Record<string, string>,
): { draft: Record<string, DraftFile>; stale: string[] } {
  const byPath = new Map(files.map((proposal) => [proposal.path, proposal]));
  const next: Record<string, DraftFile> = {};
  const stale: string[] = [];
  for (const [path, held] of Object.entries(draft)) {
    const proposed = byPath.get(path);
    if (!proposed) continue; // the new config no longer proposes this file at all
    const edited = held.body !== pristine[path];
    if (!edited) {
      next[path] = { body: proposed.body, facts: proposed.facts };
      continue;
    }
    const changed = Object.entries(held.facts).some(
      ([marker, value]) => proposed.facts[marker] !== value,
    );
    next[path] = held;
    if (changed) stale.push(path);
  }
  return { draft: next, stale };
}
