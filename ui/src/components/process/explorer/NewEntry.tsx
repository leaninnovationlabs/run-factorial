import { useState } from "react";
import { Button, TextField } from "../../ui";

/**
 * The explorer's prompt for a new file or folder: a path under the config folder, started at the
 * folder it was asked for in. It floats where the person pressed to add (`PopupAt` holds it), so
 * it carries no frame of its own. Enter creates it, Escape or Cancel puts it away; a name the
 * server would refuse is refused here as it is typed (`newPathProblem`).
 *
 * With `naming` it asks for one name instead of a path: an agent's, a skill's, an MCP server's
 * or a process's, which the caller turns into every folder and file that construct needs.
 */
export function NewEntry({
  kind,
  configDir,
  start,
  problemOf,
  onCreate,
  onCancel,
  naming,
}: {
  kind: "file" | "folder";
  configDir: string;
  /** Where the typed path starts, relative to the config folder: a folder and a slash, or "". */
  start: string;
  /** Why a full path (or, with `naming`, a name) cannot be created, or null. */
  problemOf: (path: string) => string | null;
  /** Called with the full path, under the config folder; with `naming`, with the name. */
  onCreate: (path: string) => void;
  onCancel: () => void;
  /** Ask for a construct's name: the field's label, its example and the button's words. */
  naming?: { label: string; placeholder: string; submit: string };
}) {
  const [value, setValue] = useState(start);
  const [tried, setTried] = useState(false);
  const path = naming ? value.trim() : `${configDir}/${value.trim().replace(/\/+$/, "")}`;
  const problem = value.trim() ? problemOf(path) : "Type a name.";
  const shown = tried || (value !== start && value.trim() !== "") ? problem : null;

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        setTried(true);
        if (!problem) onCreate(path);
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") onCancel();
      }}
      className="w-72 max-w-full space-y-2 p-3"
    >
      <TextField
        label={
          naming?.label ??
          (kind === "file" ? `New file in ${configDir}/` : `New folder in ${configDir}/`)
        }
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder={naming?.placeholder ?? (kind === "file" ? "folder/name.md" : "folder")}
        isMonospaced
        spacing="compact"
        autoFocus
        errorMessage={shown ?? undefined}
      />
      <div className="flex gap-2">
        <Button type="submit" appearance="primary" spacing="compact">
          {naming?.submit ?? (kind === "file" ? "Create file" : "Create folder")}
        </Button>
        <Button spacing="compact" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
