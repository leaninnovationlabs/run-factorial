/**
 * The process editor's side of an assistant's editor card: `?draft=<item id>` names a draft
 * (`GET /api/assistant/drafts/{id}`) for the editor to open on, on the lane the draft names, or on
 * a new lane when the project has no lane of that name.
 *
 * The section opens the editor with it, after a confirm when that lane already holds unsaved edits,
 * and the editor makes it the stored draft (`ProcessEditorBody`'s `initialDraft`). Whatever
 * happens, opened, declined, or a draft that cannot be had, the address forgets the draft, so a
 * reload does not load it again over what the person did since; a failure stays on the page.
 */

import { useEffect, useState } from "react";
import { useAssistantDraft } from "../../api/queries";
import type { EditorDraftPayload, ProjectDetail } from "../../api/types";
import { useViewParams } from "../../searchParams";
import type { DraftFile } from "./explorer/bundleDraft";
import type { RepoEdits } from "./explorer/repoEdits";

type EditorHandoff = {
  draft: EditorDraftPayload | null;
  /** The lane the draft is for; null for one the project does not have yet. */
  lane: string | null;
  error: unknown;
  /** Forget the draft's address. */
  done: () => void;
};

export function useEditorDraft(project: ProjectDetail): EditorHandoff {
  const [params, update] = useViewParams();
  const raw = params.get("draft");
  const itemId = raw && /^\d+$/.test(raw) ? Number(raw) : null;
  const query = useAssistantDraft(itemId);
  const fetched = itemId === null ? undefined : query.data;
  const draft = fetched?.project_id === project.id ? fetched : null;
  const lane = draft
    ? (project.processes.find((entry) => entry.process_key === draft.process)?.process_key ?? null)
    : null;
  const elsewhere = fetched !== undefined && draft === null;
  const [failed, setFailed] = useState<unknown>(null);
  // biome-ignore lint/correctness/useExhaustiveDependencies: a failure arriving is the event
  useEffect(() => {
    if (itemId === null) return;
    if (!query.error && !elsewhere) return setFailed(null);
    setFailed(query.error ?? new Error("That draft is for another project."));
    update({ draft: null });
  }, [itemId, query.error, elsewhere]);
  return { draft, lane, error: failed, done: () => update({ draft: null }) };
}

/** The draft's new files with a body of their own, as the explorer holds an edited proposal. A
 *  file without one takes the catalog's, which the explorer proposes by itself. */
export function draftFiles(draft: EditorDraftPayload): Record<string, DraftFile> {
  const files: Record<string, DraftFile> = {};
  for (const file of draft.files) {
    if (file.body !== null) files[file.path] = { body: file.body, facts: {} };
  }
  return files;
}

export function draftEdits(draft: EditorDraftPayload): RepoEdits {
  return Object.fromEntries(
    draft.edits.map((edit) => [edit.path, { body: edit.body, base: edit.base_sha }]),
  );
}
