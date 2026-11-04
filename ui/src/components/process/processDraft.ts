/**
 * The editor's uncommitted draft, persisted per project AND process so leaving the page costs
 * nothing. localStorage over a leave-warning: a dialog guards against loss; persistence removes
 * it.
 *
 * The process is part of the key because a project runs several, and a key holding the project
 * alone would restore one lane's draft into another — silently, and into the editor that commits to
 * `factorial`. A draft written before the key grew a process belongs to whichever lane the
 * project had then, so it is read once under the old key and rewritten under the new one on the first save.
 *
 * Drafts record the COMMIT they were written against: a draft taken from a1b2c3d and reopened
 * after 9f8e7d6 was synced must present as "a copy of a1b2c3d" so the newer-commit warning
 * fires — not silently wear the new one.
 */

import { safeGet, safeSet } from "../../storage";

export interface StoredDraft {
  sha: string | null;
  text: string;
}

/** A draft for a process the operator has not created yet: the editor is writing a NEW lane, so
 *  there is no key to file it under and every unsaved new process shares this one. */
export const NEW_PROCESS = "\u0000new";

export const key = (projectId: number, processKey: string | null) =>
  `sf.processDraft.${projectId}.${processKey ?? NEW_PROCESS}`;

/** Where a draft written before the key grew a process lives. Read once, then rewritten under
 *  the new key by the first save, so nobody loses work on the upgrade. */
const legacyKey = (projectId: number) => `sf.processDraft.${projectId}`;

export function readDraft(projectId: number, processKey: string | null): StoredDraft | null {
  const stored = safeGet(key(projectId, processKey)) ?? safeGet(legacyKey(projectId));
  if (stored === null) return null;
  try {
    const parsed = JSON.parse(stored);
    if (parsed && typeof parsed.text === "string") {
      return {
        // A draft written when snapshots were numbered carries `version`, which names nothing
        // now. Reading it as "no commit" is right: the draft is still the operator's text, and
        // the warning it drives is about a commit it cannot claim to know.
        sha: typeof parsed.sha === "string" ? parsed.sha : null,
        text: parsed.text,
      };
    }
  } catch {
    // The oldest drafts were the raw string; nothing is known about where they came from.
  }
  return { sha: null, text: stored };
}

export function writeDraft(
  projectId: number,
  processKey: string | null,
  sha: string | null,
  text: string,
): void {
  safeSet(key(projectId, processKey), JSON.stringify({ sha, text }));
}

export function clearDraft(projectId: number, processKey: string | null): void {
  safeSet(key(projectId, processKey), null);
  safeSet(legacyKey(projectId), null);
}

/** What a fresh editor opens on: a surviving draft beats the baseline, and carries ITS commit
 *  so the caption tells the truth; a draft identical to the baseline is stale bookkeeping. */
export function resolveSeed(
  baseline: string,
  currentSha: string | null,
  draft: StoredDraft | null,
): { text: string; seededSha: string | null } {
  if (draft !== null && draft.text !== baseline) {
    return { text: draft.text, seededSha: draft.sha ?? currentSha };
  }
  return { text: baseline, seededSha: currentSha };
}
