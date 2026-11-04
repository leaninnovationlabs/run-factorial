/**
 * The config explorer's tree: `.factorial/` paths nested by segment, from two sources that overlap.
 *
 * A path can be a file the pinned commit HAS (`repo`), a file a commit from the edits on
 * screen WOULD WRITE (`proposal`), or both — an operator editing a config whose agents are already
 * scaffolded sees one node per path, not two. Merging here rather than in the renderer is what
 * keeps "is this file in the repo" a single answer.
 *
 * Pure and total over its own output. What is worth pinning with tests is the ORDERING rule
 * (directories before files, each group alphabetical) and the tick arithmetic, both of which a
 * renderer must get exactly right without re-deriving them per call site.
 */

import type { ConfigFile, FileProposal } from "../../../api/types";

export interface ExplorerFile {
  path: string;
  /** The pinned commit's copy, when the commit has this file. */
  repo?: ConfigFile;
  /** What a commit from the edits on screen would write here, when it would. */
  proposal?: FileProposal;
}

export interface TreeNode {
  name: string;
  path: string;
  kind: "dir" | "file";
  children: TreeNode[];
  file?: ExplorerFile;
}

export type Tick = "on" | "off" | "partial";

interface MutableNode {
  name: string;
  path: string;
  file?: ExplorerFile;
  children: Map<string, MutableNode>;
}

/** One entry per path across both sources, sorted, each carrying whichever halves exist. */
export function mergeFiles(repo: ConfigFile[], proposals: FileProposal[]): ExplorerFile[] {
  const merged = new Map<string, ExplorerFile>();
  for (const file of repo) merged.set(file.path, { path: file.path, repo: file });
  for (const proposal of proposals) {
    const existing = merged.get(proposal.path);
    if (existing) existing.proposal = proposal;
    else merged.set(proposal.path, { path: proposal.path, proposal });
  }
  return [...merged.values()].sort((a, b) => a.path.localeCompare(b.path));
}

/** `folders` are folders that hold no file yet — made in the editor, and drawn empty. */
export function buildTree(files: ExplorerFile[], folders: string[] = []): TreeNode[] {
  const root: MutableNode = { name: "", path: "", children: new Map() };
  for (const folder of folders) {
    let node = root;
    let path = "";
    for (const segment of folder.split("/").filter(Boolean)) {
      path = path ? `${path}/${segment}` : segment;
      let child = node.children.get(segment);
      if (!child) {
        child = { name: segment, path, children: new Map() };
        node.children.set(segment, child);
      }
      node = child;
    }
  }
  for (const file of files) {
    const segments = file.path.split("/").filter(Boolean);
    let node = root;
    let path = "";
    segments.forEach((segment, index) => {
      path = path ? `${path}/${segment}` : segment;
      let child = node.children.get(segment);
      if (!child) {
        child = { name: segment, path, children: new Map() };
        node.children.set(segment, child);
      }
      if (index === segments.length - 1) child.file = file;
      node = child;
    });
  }
  return freezeChildren(root.children);
}

function freezeChildren(children: Map<string, MutableNode>): TreeNode[] {
  return Array.from(children.values()).map(freeze).sort(compareNodes);
}

function freeze(node: MutableNode): TreeNode {
  return {
    name: node.name,
    path: node.path,
    kind: node.file ? "file" : "dir",
    children: freezeChildren(node.children),
    file: node.file,
  };
}

function compareNodes(a: TreeNode, b: TreeNode): number {
  if (a.kind !== b.kind) return a.kind === "dir" ? -1 : 1;
  return a.name.localeCompare(b.name);
}

/** Every file path reachable beneath `node`, itself included when it is a file. */
export function pathsUnder(node: TreeNode): string[] {
  if (node.kind === "file") return [node.path];
  return node.children.flatMap(pathsUnder);
}

/**
 * The paths a tick may legally add: proposed, and not already in the repo.
 *
 * `assemble.py` refuses any picked path the repo already owns, so a file the branch has is never
 * a member of a submitted selection — it reaches a commit only as an edit, and a file with no
 * proposal only that way too. Counting either of them would leave a folder of untickable files
 * stuck on `partial` however many times the operator clicked it.
 */
export function selectableUnder(node: TreeNode): string[] {
  if (node.kind === "file") return isSelectable(node.file) ? [node.path] : [];
  return node.children.flatMap(selectableUnder);
}

export function isSelectable(file: ExplorerFile | undefined): boolean {
  return Boolean(file?.proposal && !file.proposal.present_in_repo);
}

export type ChangeKind = "added" | "modified";

/**
 * What the commit would do to this node: create it, change a file the repo already has, or
 * nothing. A directory takes the strongest answer beneath it, because a folder holding both a new
 * file and a changed one is a folder something is being changed in.
 *
 * "Added" is the ticked proposals — unticking a file is how an operator says the commit should
 * not write it — plus `created`: a new lane's own process file, which every commit writes and
 * no tick governs. "Modified" is handed down rather than derived: a repo file is rewritten only
 * from the editor's own state — the lane's process text, or a held edit of the branch's copy —
 * never from a proposal.
 */
export function changeOf(
  node: TreeNode,
  selected: Set<string>,
  modified: Set<string>,
  created: Set<string> = EMPTY,
): ChangeKind | null {
  if (node.kind === "file") {
    if (modified.has(node.path)) return "modified";
    if (created.has(node.path)) return "added";
    return isSelectable(node.file) && selected.has(node.path) ? "added" : null;
  }
  const beneath = node.children.map((child) => changeOf(child, selected, modified, created));
  if (beneath.includes("modified")) return "modified";
  return beneath.includes("added") ? "added" : null;
}

/** `"off"` for a directory with nothing selectable under it: nothing is selected because there is
 *  nothing to select, which is the same state as everything having been unticked. */
export function tickOf(node: TreeNode, selected: Set<string>): Tick {
  const paths = selectableUnder(node);
  if (paths.length === 0) return "off";
  const selectedCount = paths.filter((path) => selected.has(path)).length;
  if (selectedCount === 0) return "off";
  if (selectedCount === paths.length) return "on";
  return "partial";
}

const PROCESSES_DIR = "processes";
const PROCESS_PREFIX = "process-";
const PROCESS_SUFFIX = ".yaml";

/** Mirrors `orchestrator/config/loader.py`'s `process_file_name`; change the two together. */
export function lanePath(configDir: string, name: string): string {
  return `${configDir}/${PROCESSES_DIR}/${PROCESS_PREFIX}${name}${PROCESS_SUFFIX}`;
}

export const EMPTY: Set<string> = new Set();

/** Open to the folders, closed to nothing: a `.factorial/` is a dozen files, and an explorer that
 *  opens collapsed makes the reader click three times to see what they came for. */
/** The folders the explorer opens on: the top level only, so the config folder shows its own
 *  folders and files, each closed until asked for. */
export function topLevelExpanded(files: ExplorerFile[]): Set<string> {
  return new Set(files.map((file) => file.path.split("/")[0] ?? "").filter(Boolean));
}

export function defaultExpanded(files: ExplorerFile[]): Set<string> {
  const dirs = new Set<string>();
  for (const file of files) {
    const segments = file.path.split("/");
    for (let i = 1; i < segments.length; i += 1) dirs.add(segments.slice(0, i).join("/"));
  }
  return dirs;
}

export function toggled(set: Set<string>, path: string, on: boolean): Set<string> {
  const next = new Set(set);
  if (on) next.add(path);
  else next.delete(path);
  return next;
}
