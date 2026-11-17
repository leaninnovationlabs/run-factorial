import { type ReactNode, type RefObject, useId } from "react";
import type { ProcessTemplate, ProcessValidateResult } from "../../api/types";
import { Button, DropdownMenu, Icon } from "../ui";
import type { CommitButtonState } from "./commitGate";

export type EditorHeading = {
  title: ReactNode;
  /** The standing note under the title, while the commit has nothing to say. */
  subtitle: ReactNode;
  onBack: () => void;
};

function downloadYaml(text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/yaml" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "process.yaml";
  anchor.click();
  // Deferred: a synchronous revoke can race the download engaging in some browsers.
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

/** The file menu (Import a file, Export, Copy) is not offered. Its items and the file input stay
 *  wired, so the menu comes back whole when this is true. */
const FILE_MENU_OFFERED: boolean = false;

/** The row over the editor: the way back and what is being edited, with what the commit has to
 *  say under that heading; then the commit button, the config's verdict and the whole-config
 *  actions. With nothing to commit the button says so itself, rather than a reason beside it.
 *  `readOnly` keeps the way back alone.
 *
 *  The verdict chip is the process file's own check, made as it is typed. Validate is the whole
 *  draft's: every edited, new and deleted file, judged as a commit would judge it. */
export function EditorActions({
  readOnly = false,
  heading,
  commitButton,
  onCommit,
  onValidate,
  validating = false,
  unpermittedSlugs,
  onPermitAndCommit,
  text,
  checkingValidity,
  verdict,
  editedCount,
  unchanged,
  changed,
  onDiscard,
  templates,
  onTemplate,
  onCopy,
  importInput,
  onImport,
}: {
  readOnly?: boolean;
  heading?: EditorHeading;
  commitButton: CommitButtonState;
  onCommit: () => void;
  /** Runs the commit's own checks on every file of the draft, and commits nothing. */
  onValidate?: () => void;
  validating?: boolean;
  /** E2B runtimes the config names that the project has not permitted. */
  unpermittedSlugs: string[];
  onPermitAndCommit: () => void;
  /** The process text. */
  text: string;
  checkingValidity: boolean;
  verdict: ProcessValidateResult | undefined;
  /** Files with edits the next commit carries, the process file among them. */
  editedCount: number;
  /** Nothing a commit would carry differs from the pin. */
  unchanged: boolean;
  /** Anything Discard edits would undo: the text, an edited body, a moved tick. */
  changed: boolean;
  onDiscard: () => void;
  templates: ProcessTemplate[];
  /** Replaces the process text with a template's process. */
  onTemplate: (template: string) => void;
  onCopy: () => void;
  importInput: RefObject<HTMLInputElement | null>;
  /** Called with the imported file's text. */
  onImport: (text: string) => void;
}) {
  async function importYaml(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    // Reset so picking the same file again still fires a change event.
    event.target.value = "";
    if (!file) return;
    onImport(await file.text());
  }

  const verdictLabel =
    verdict &&
    (verdict.valid
      ? verdict.warnings.length
        ? `Valid, ${verdict.warnings.length} warning${verdict.warnings.length === 1 ? "" : "s"}`
        : "Valid config"
      : "Invalid config");

  const reasonId = useId();
  // Written under the heading rather than beside the button, as `Button`'s `disabledReason` would.
  const blocked = !readOnly && Boolean(commitButton.reason) && !unchanged;
  const idle = unchanged && !commitButton.isLoading;

  const messages = [
    !readOnly && editedCount > 0 && (
      <span key="edited">
        {editedCount} {editedCount === 1 ? "file" : "files"} edited
      </span>
    ),
    !readOnly && unpermittedSlugs.length > 0 && (
      <span key="permit" className="text-warning">
        {unpermittedSlugs.join(", ")} {unpermittedSlugs.length === 1 ? "isn't" : "aren't"} permitted
        for this project
      </span>
    ),
    blocked && (
      <span key="reason" id={reasonId}>
        {commitButton.reason}
      </span>
    ),
  ].filter(Boolean);

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-default px-4 py-3">
      <div className="flex min-w-0 items-center gap-3">
        {heading && (
          <Button iconBefore="arrow-left" onClick={heading.onBack}>
            Back
          </Button>
        )}
        <div className="min-w-0">
          {heading && <h2 className="text-heading-sm text-default">{heading.title}</h2>}
          {/* What the commit has to say, under the heading; the standing note when it has nothing. */}
          <p className="flex flex-wrap gap-x-3 text-body-sm text-subtle">
            {messages.length > 0 ? messages : heading?.subtitle}
          </p>
        </div>
      </div>
      <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
        {!readOnly && (
          <>
            <Button
              data-assist-anchor="process.commit"
              appearance={idle ? "default" : "primary"}
              iconBefore={idle ? "check" : undefined}
              isLoading={commitButton.isLoading}
              aria-disabled={idle || blocked || undefined}
              aria-describedby={blocked ? reasonId : undefined}
              onClick={onCommit}
            >
              {idle ? "No changes to commit" : "Commit and sync"}
            </Button>
            {unpermittedSlugs.length > 0 && (
              <Button
                isLoading={commitButton.isLoading}
                aria-disabled={blocked || undefined}
                aria-describedby={blocked ? reasonId : undefined}
                onClick={onPermitAndCommit}
              >
                Permit and commit
              </Button>
            )}
            {onValidate && text.trim() && (
              <Button isLoading={validating} onClick={onValidate}>
                Validate
              </Button>
            )}
            {text.trim() && !checkingValidity && verdict && (
              // Plain words at a button's height, so the row reads as one; the icon carries the verdict.
              <span className="inline-flex h-8 items-center gap-1.5 rounded-md border border-default px-3 text-body text-default">
                {verdict.valid && !verdict.warnings.length ? (
                  <Icon glyph="status-success" className="text-icon-success" />
                ) : verdict.valid ? (
                  <Icon glyph="status-warning" className="text-icon-warning" />
                ) : (
                  <Icon glyph="status-error" className="text-icon-danger" />
                )}
                {verdictLabel}
              </span>
            )}
            {changed && (
              <Button iconBefore="undo" onClick={onDiscard}>
                Discard edits
              </Button>
            )}
            <DropdownMenu
              trigger={{ label: "Import" }}
              items={templates.map((template) => ({
                label: template.title,
                description: template.description,
                onClick: () => onTemplate(template.key),
              }))}
            />
          </>
        )}
        {FILE_MENU_OFFERED && (
          <DropdownMenu
            trigger={{ label: "Editor actions", icon: "more", iconOnly: true }}
            items={[
              ...(readOnly
                ? []
                : [{ label: "Import a file", onClick: () => importInput.current?.click() }]),
              {
                label: "Export",
                description: "Download process.yaml",
                unavailable: text.trim() ? undefined : "Nothing to export yet",
                onClick: () => downloadYaml(text),
              },
              {
                label: "Copy",
                unavailable: text.trim() ? undefined : "Nothing to copy yet",
                onClick: onCopy,
              },
            ]}
          />
        )}
      </div>
      {FILE_MENU_OFFERED && !readOnly && (
        <input
          ref={importInput}
          type="file"
          accept=".yaml,.yml,text/yaml"
          aria-label="Import process.yaml"
          onChange={importYaml}
          hidden
        />
      )}
    </div>
  );
}
