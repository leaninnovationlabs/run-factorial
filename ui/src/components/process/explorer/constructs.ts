/**
 * The four things a `.factorial/` folder is made of, as the explorer creates them: which of them
 * a folder offers, where each one's files go, and the names each may take.
 *
 * The layout is the loader's (`orchestrator/config/loader.py`) and the name rules are the
 * models' (`orchestrator/config/models.py`); change them together. A construct is nothing more
 * than new files: the server holds each to the created-file rule and checks the folder again at
 * the commit, so a refusal here only says so sooner.
 */

import type { Glyph } from "../../ui";
import { LANE_KEY } from "../laneName";
import { SAFE_NAME, UNSAFE_NAME } from "./createdFiles";
import { lanePath } from "./tree";

export type ConstructKind = "agent" | "skill" | "mcp" | "process";

export interface Construct {
  kind: ConstructKind;
  /** The owner of a skill or MCP server that belongs to one agent. */
  agent?: string;
}

/** Kind to file name inside that shell to body, as `GET /process-templates` serves them. */
export type Shells = Record<string, Record<string, string>>;

const NOUN: Record<ConstructKind, string> = {
  agent: "agent",
  mcp: "MCP server",
  process: "process",
  skill: "skill",
};

/** `mcp__<server>__<tool>`: the name becomes a tool prefix (`models.py`'s `_MCP_NAME`). */
const MCP_NAME = /^[a-z0-9][a-z0-9-]*$/;
const MARKER = "__NAME__";
const MANIFEST = "agent.yaml";

/** What the construct is called in a menu that already says "Add new". */
export function constructName(construct: Construct): string {
  const noun = NOUN[construct.kind];
  return noun.charAt(0).toUpperCase() + noun.slice(1);
}

const ICON: Record<ConstructKind, Glyph> = {
  agent: "agent",
  mcp: "plug",
  process: "lane",
  skill: "file-markdown",
};

export function constructIcon(construct: Construct): Glyph {
  return ICON[construct.kind];
}

/** What the prompt asks for: the construct, and whose it is when it belongs to one agent. */
export function constructLabel(construct: Construct): string {
  return `New ${NOUN[construct.kind]}${construct.agent ? ` for ${construct.agent}` : ""}`;
}

/** What a folder offers, in the order the menu lists it. */
export function constructsFor(folder: string, configDir: string): Construct[] {
  if (folder === configDir) {
    return [{ kind: "agent" }, { kind: "mcp" }, { kind: "process" }, { kind: "skill" }];
  }
  if (!folder.startsWith(`${configDir}/`)) return [];
  const segments = folder.slice(configDir.length + 1).split("/");
  const [top, agent, sub] = segments;
  if (top === "agents") {
    if (agent === undefined) return [{ kind: "agent" }];
    if (sub === undefined) {
      return [
        { kind: "mcp", agent },
        { kind: "skill", agent },
      ];
    }
    if (sub === "skills") return [{ kind: "skill", agent }];
    return sub === "mcp" && segments.length === 3 ? [{ kind: "mcp", agent }] : [];
  }
  if (top === "skills") return [{ kind: "skill" }];
  if (segments.length > 1) return [];
  if (top === "mcp") return [{ kind: "mcp" }];
  return top === "processes" ? [{ kind: "process" }] : [];
}

/** The folder a construct's own folder or file is made in. */
export function constructFolder(construct: Construct, configDir: string): string {
  const { kind, agent } = construct;
  if (kind === "agent") return `${configDir}/agents`;
  if (kind === "process") return `${configDir}/processes`;
  const owner = agent ? `${configDir}/agents/${agent}` : configDir;
  return `${owner}/${kind === "skill" ? "skills" : "mcp"}`;
}

/** Where a construct lands: a folder of its own, or one file. */
function target(construct: Construct, configDir: string, name: string): string {
  const folder = constructFolder(construct, configDir);
  if (construct.kind === "process") return lanePath(configDir, name);
  return construct.kind === "mcp" ? `${folder}/${name}.yaml` : `${folder}/${name}`;
}

/** Full path to body, the name filled in throughout; the first is the file to open. Empty when
 *  the server sent no shell for the kind. */
export function constructFiles(
  shells: Shells,
  construct: Construct,
  configDir: string,
  name: string,
): Record<string, string> {
  const shell = shells[construct.kind];
  if (!shell) return {};
  const at = target(construct, configDir, name);
  const fill = (body: string) => body.replaceAll(MARKER, name);
  if (construct.kind === "mcp" || construct.kind === "process") {
    const [body] = Object.values(shell);
    return body === undefined ? {} : { [at]: fill(body) };
  }
  const names = Object.keys(shell).sort(
    (a, b) => Number(b === MANIFEST) - Number(a === MANIFEST) || a.localeCompare(b),
  );
  return Object.fromEntries(names.map((file) => [`${at}/${file}`, fill(shell[file] ?? "")]));
}

function ruleProblem(kind: ConstructKind, name: string): string | null {
  if (kind === "mcp") {
    return MCP_NAME.test(name)
      ? null
      : "Use lowercase letters, digits and dashes. The name becomes the tool prefix.";
  }
  if (kind === "process") {
    if (name === "default") return "The name default is reserved.";
    return LANE_KEY.test(name)
      ? null
      : "Use lowercase letters, digits and dashes, starting and ending with a letter or digit.";
  }
  return SAFE_NAME.test(name) ? null : UNSAFE_NAME;
}

/**
 * Why `name` cannot be this construct's, or null when it can. `taken` is every file path the
 * explorer knows; `proposed` the paths the catalog offers for the config on screen.
 */
export function constructNameProblem(
  construct: Construct,
  name: string,
  configDir: string,
  taken: string[],
  proposed: string[],
): string | null {
  const rule = ruleProblem(construct.kind, name);
  if (rule) return rule;
  const at = target(construct, configDir, name);
  const within = (path: string) => path === at || path.startsWith(`${at}/`);
  // Before `taken`, which lists the catalog's offers too: a template is ticked, not created.
  if (proposed.some(within)) return `${name} has a template. Tick its files in the explorer.`;
  if (taken.some(within)) {
    const noun = NOUN[construct.kind];
    return `${/^[aeiouM]/.test(noun) ? "An" : "A"} ${noun} with that name is already here.`;
  }
  return null;
}

/** A manifest with no line that is neither blank nor a comment: the stub a commit writes for an
 *  agent it has no template for. */
export function isEmptyManifest(text: string): boolean {
  return text.split("\n").every((line) => {
    const trimmed = line.trim();
    return trimmed === "" || trimmed.startsWith("#");
  });
}
