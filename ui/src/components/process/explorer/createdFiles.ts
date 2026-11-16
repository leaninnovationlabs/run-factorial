/**
 * Files the operator created in the editor, persisted per project and process like the other
 * drafts (`repoEdits.ts`), and the rule the server holds their paths to (tech-stack decision 33):
 * under the config folder, every segment a safe name, and nothing already at the path, above it or
 * inside it. Checked here as the name is typed, so a refusal names the problem before the commit.
 *
 * A new folder is only a path prefix: git holds no empty folder, so one is written once a file is
 * created inside it, and the explorer forgets an empty one on reload.
 */

import { safeGet, safeSet } from "../../../storage";
import { NEW_PROCESS } from "../processDraft";

/** Path to body. */
export type CreatedFiles = Record<string, string>;

const key = (projectId: number, processKey: string | null) =>
  `sf.createdFiles.${projectId}.${processKey ?? NEW_PROCESS}`;

/** Total, like `parseRepoEdits`: a stored value outlives the app that wrote it. */
export function parseCreatedFiles(raw: string | null): CreatedFiles {
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

export function readCreatedFiles(projectId: number, processKey: string | null): CreatedFiles {
  return parseCreatedFiles(safeGet(key(projectId, processKey)));
}

export function writeCreatedFiles(
  projectId: number,
  processKey: string | null,
  files: CreatedFiles,
): void {
  safeSet(key(projectId, processKey), Object.keys(files).length > 0 ? JSON.stringify(files) : null);
}

/** `assemble.py`'s `_SAFE_NAME`: no `..`, no dot-folder, no space or slash. */
export const SAFE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

export const UNSAFE_NAME =
  "Each folder and file name starts with a letter or digit, then letters, digits, dots, dashes or underscores.";

/**
 * Why `path` cannot be created as a `kind`, or null when it can. `taken` is every file path the
 * explorer knows; `folders` the folders made here that hold nothing yet.
 */
export function newPathProblem(
  path: string,
  kind: "file" | "folder",
  configDir: string,
  taken: string[],
  folders: string[],
): string | null {
  const prefix = `${configDir}/`;
  const segments = path.startsWith(prefix) ? path.slice(prefix.length).split("/") : [];
  if (segments.length === 0 || segments.some((segment) => !SAFE_NAME.test(segment))) {
    return UNSAFE_NAME;
  }
  if (taken.includes(path)) return "A file with that name is already here.";
  const ancestors = segments
    .slice(0, -1)
    .map((_, end) => prefix + segments.slice(0, end + 1).join("/"));
  if (ancestors.some((ancestor) => taken.includes(ancestor))) {
    return "Part of that path is a file, not a folder.";
  }
  const isFolder = folders.includes(path) || taken.some((other) => other.startsWith(`${path}/`));
  if (isFolder)
    return kind === "folder"
      ? "That folder is already here."
      : "A folder with that name is already here.";
  return null;
}
