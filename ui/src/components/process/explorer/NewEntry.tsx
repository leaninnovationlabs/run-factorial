import { useState } from "react";
import { Button, TextField } from "../../ui";

/**
 * The explorer's inline form for a new file or folder: a path under the config folder, started at
 * the folder of the file on show. Enter creates it, Escape or Cancel puts the form away; a name
 * the server would refuse is refused here as it is typed (`newPathProblem`).
 */
export function NewEntry({
  kind,
  configDir,
  start,
  problemOf,
  onCreate,
  onCancel,
}: {
  kind: "file" | "folder";
  configDir: string;
  /** Where the typed path starts, relative to the config folder: a folder and a slash, or "". */
  start: string;
  /** Why a full path cannot be created, or null. */
  problemOf: (path: string) => string | null;
  /** Called with the full path, under the config folder. */
  onCreate: (path: string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(start);
  const [tried, setTried] = useState(false);
  const path = `${configDir}/${value.trim().replace(/\/+$/, "")}`;
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
      className="mb-3 space-y-2 rounded-md border border-default p-2"
    >
      <TextField
        label={kind === "file" ? `New file in ${configDir}/` : `New folder in ${configDir}/`}
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder={kind === "file" ? "folder/name.md" : "folder"}
        isMonospaced
        spacing="compact"
        autoFocus
        errorMessage={shown ?? undefined}
      />
      <div className="flex gap-2">
        <Button type="submit" appearance="primary" spacing="compact">
          {kind === "file" ? "Create file" : "Create folder"}
        </Button>
        <Button spacing="compact" appearance="subtle" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
