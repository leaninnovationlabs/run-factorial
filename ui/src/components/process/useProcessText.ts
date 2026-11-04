import { useEffect, useRef, useState } from "react";
import { useProcessVersionRaw } from "../../api/queries";
import type { ProjectDetail } from "../../api/types";
import { clearDraft, readDraft, resolveSeed, writeDraft } from "./processDraft";

/** The editor's text: seeded once from what the lane runs or a surviving draft, and kept as a
 *  draft while it differs from that seed; `readOnly` keeps no draft. */
export function useProcessText(
  project: ProjectDetail,
  processKey: string | null,
  readOnly = false,
) {
  const [text, setText] = useState("");
  // The baseline is what the project actually runs: the pinned version's raw text (comments
  // intact when the repo file is readable), or empty for a fresh project. Seeded exactly
  // once. A surviving draft beats the baseline and keeps ITS version, so the toolbar's
  // newer-pin warning can fire; if the raw read errors, the draft alone seeds — the user's work
  // must not be held hostage to a GitHub outage.
  const pinned =
    project.processes.find((entry) => entry.process_key === processKey)?.latest ?? null;
  const raw = useProcessVersionRaw(project.id, pinned?.id);
  const seeded = useRef(false);
  const [baseline, setBaseline] = useState<string | null>(null);
  const [seededSha, setSeededSha] = useState<string | null>(null);
  useEffect(() => {
    if (seeded.current) return;
    const draft = readOnly ? null : readDraft(project.id, processKey);
    let base: string | undefined;
    if (!pinned) base = "";
    else if (raw.data) base = raw.data.content;
    else if (raw.isError) base = draft?.text ?? "";
    if (base === undefined) return;
    seeded.current = true;
    setBaseline(base);
    const seed = resolveSeed(
      base,
      raw.isError ? (draft?.sha ?? null) : (pinned?.source_commit_sha ?? null),
      draft,
    );
    setSeededSha(seed.seededSha);
    // Never clobber text the user typed, pasted, imported or prefilled while the fetch was
    // in flight — the seed only fills an empty editor.
    setText((current) => current || seed.text);
  }, [raw.data, raw.isError, pinned, project.id, processKey, readOnly]);

  // The draft IS the persistence: written (debounced) while dirty, gone when the text matches
  // the baseline again. Leaving the page loses nothing, so no leave-warning exists to dismiss.
  const dirty = baseline !== null && text !== baseline;
  useEffect(() => {
    if (baseline === null || readOnly) return;
    const timer = setTimeout(() => {
      if (text !== baseline) writeDraft(project.id, processKey, seededSha, text);
      else clearDraft(project.id, processKey);
    }, 400);
    return () => clearTimeout(timer);
  }, [text, baseline, seededSha, project.id, processKey, readOnly]);

  /** A commit that synced is what the lane runs now, so it becomes the baseline. */
  function rebase(committed: string, sha: string) {
    setBaseline(committed);
    setSeededSha(sha);
    clearDraft(project.id, processKey);
  }

  return { text, setText, raw, pinned, baseline, seededSha, dirty, rebase };
}
