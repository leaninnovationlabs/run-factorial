/**
 * The editor's right half for any file but `process.yaml`: one file's body, read or edited, edge
 * to edge under its tab. Its path is the tab's to say; what kind of file it is, and the way back
 * to its original, sit in one thin row over it.
 *
 * `process.yaml` never reaches it: its text belongs to the editor, not to a proposal.
 * `assemble.py` commits it from `yaml_text` and never from the selection. Two kinds of file do:
 *
 * - a proposed file — editable, and its edits are the bundle draft.
 * - a file the branch already has — edited from its own copy while the pin and the head agree on
 *   it (the edit carries the blob it started from), read-only otherwise.
 */

import { Button, CodeEditor, SectionMessage, Spinner } from "../../ui";
import type { ExplorerFile } from "./tree";

export function FilePane({
  file,
  body,
  loading,
  stale,
  onChange,
  onRevert,
  onConfirm,
  lockReason,
  revertLabel = "Revert to template",
  isNew = false,
}: {
  file: ExplorerFile;
  body: string;
  /** The body is still being read from the repo. */
  loading?: boolean;
  /** The facts this body was written against have since moved. Reverting clears it; the operator
   *  can also keep the edit as-is with the confirm below. */
  stale: boolean;
  /** Absent makes the pane read-only — a repo file, or the read-only drawer. */
  onChange?: (body: string) => void;
  onRevert?: () => void;
  onConfirm?: () => void;
  /** Why an otherwise editable repo file is read-only here — the pin and the branch head disagree
   *  on it. */
  lockReason?: string;
  revertLabel?: string;
  /** Created in the editor: on no branch and in no proposal yet. */
  isNew?: boolean;
}) {
  const proposal = file.proposal;
  const markers = Object.keys(proposal?.facts ?? {});
  const editable = Boolean(onChange);
  const about = isNew
    ? "New file. The next commit writes it."
    : describe(file, editable, lockReason);
  return (
    <div className="flex h-full min-h-0 flex-col">
      {(about || onRevert) && (
        <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-default px-3 py-1.5 text-body-sm text-subtle">
          <span>{about}</span>
          {onRevert && (
            <span className="ml-auto">
              <Button spacing="compact" appearance="subtle" iconBefore="undo" onClick={onRevert}>
                {revertLabel}
              </Button>
            </span>
          )}
        </div>
      )}

      {(file.repo?.warning || stale) && (
        <div className="shrink-0 space-y-2 border-b border-default p-3">
          {file.repo?.warning && (
            <SectionMessage appearance="warning">{file.repo.warning}</SectionMessage>
          )}
          {stale && (
            <SectionMessage
              appearance="warning"
              title={
                file.repo
                  ? "This file changed on the factorial branch since you started editing it"
                  : "The config changed since this was edited"
              }
              actions={
                onConfirm
                  ? [
                      {
                        text: file.repo ? "Use my edit anyway" : "Use this version anyway",
                        onClick: onConfirm,
                      },
                    ]
                  : undefined
              }
            >
              {file.repo
                ? onConfirm
                  ? undefined
                  : "Sync config, then keep your edit or revert it."
                : markers.length > 0
                  ? `This file reads ${markers.join(", ")}, and one of those values has moved.`
                  : undefined}
            </SectionMessage>
          )}
        </div>
      )}

      {loading ? (
        <div className="p-4">
          <Spinner />
        </div>
      ) : (
        <CodeEditor
          label={file.path}
          value={body}
          onChange={onChange}
          readOnly={!editable}
          className="min-h-0 flex-1"
        />
      )}
    </div>
  );
}

function describe(file: ExplorerFile, editable: boolean, lockReason?: string): string {
  if (lockReason) return lockReason;
  if (file.repo)
    return editable
      ? "In the repository. Your edit replaces it in the next commit."
      : "In the repository at the commit this version was synced from. Read-only.";
  const proposal = file.proposal;
  if (!proposal) return "";
  return proposal.variant ? `${proposal.component} · ${proposal.variant}` : proposal.component;
}
