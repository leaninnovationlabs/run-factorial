import { describe, expect, it } from "vitest";
import type { ConfigFile, FileProposal } from "../../../api/types";
import {
  buildTree,
  changeOf,
  type ExplorerFile,
  mergeFiles,
  pathsUnder,
  selectableUnder,
  type TreeNode,
  tickOf,
} from "./tree";

const proposal = (path: string, over: Partial<FileProposal> = {}): FileProposal => ({
  path,
  body: "",
  component: "plan-agent",
  kind: "agent",
  variant: "base-target",
  version: 1,
  facts: {},
  present_in_repo: false,
  ...over,
});

const repo = (path: string, over: Partial<ConfigFile> = {}): ConfigFile => ({
  path,
  kind: "agent",
  drift: "same",
  warning: null,
  sha: "b".repeat(40),
  ...over,
});

/** A path a commit would write — the only kind a tick may add. */
const file = (path: string, over: Partial<FileProposal> = {}): ExplorerFile => ({
  path,
  proposal: proposal(path, over),
});

const FILES = [
  file(".factorial/agents/plan-agent/agent.yaml"),
  file(".factorial/agents/plan-agent/system-prompt.md"),
  file(".factorial/skills/project-constitution/SKILL.md"),
];

/** `noUncheckedIndexedAccess` makes every array read `T | undefined`; the tests below index into
 *  trees they just built, so a missing element is a bug in `buildTree`, not a case to handle. */
function must<T>(value: T | undefined): T {
  if (value === undefined) throw new Error("expected a value, got undefined");
  return value;
}

function childNamed(node: TreeNode, name: string): TreeNode {
  return must(node.children.find((child) => child.name === name));
}

describe("buildTree", () => {
  it("nests by path segment under a single .factorial root", () => {
    const root = must(buildTree(FILES)[0]);
    expect(root.name).toBe(".factorial");
    expect(root.children.map((c) => c.name)).toEqual(["agents", "skills"]);
  });

  it("puts directories before files at each level", () => {
    const withLocal = [...FILES, file(".factorial/agents/plan-agent/skills/x/SKILL.md")];
    const root = must(buildTree(withLocal)[0]);
    const agent = childNamed(childNamed(root, "agents"), "plan-agent");
    expect(must(agent.children[0]).kind).toBe("dir");
    expect(must(agent.children.at(-1)).kind).toBe("file");
  });

  it("sorts each group alphabetically rather than by discovery order", () => {
    const files = [
      file(".factorial/skills/z-skill/SKILL.md"),
      file(".factorial/skills/a-skill/SKILL.md"),
    ];
    const root = must(buildTree(files)[0]);
    const skills = childNamed(root, "skills");
    expect(skills.children.map((c) => c.name)).toEqual(["a-skill", "z-skill"]);
  });

  it("attaches the file to the leaf node it came from", () => {
    const root = must(buildTree(FILES)[0]);
    const agentYaml = childNamed(
      childNamed(childNamed(root, "agents"), "plan-agent"),
      "agent.yaml",
    );
    expect(agentYaml.kind).toBe("file");
    expect(agentYaml.file?.path).toBe(".factorial/agents/plan-agent/agent.yaml");
  });
});

describe("tickOf", () => {
  const root = must(buildTree(FILES)[0]);

  it("is off when nothing under it is selected", () => {
    expect(tickOf(root, new Set())).toBe("off");
  });

  it("is on when every file under it is selected", () => {
    expect(tickOf(root, new Set(FILES.map((f) => f.path)))).toBe("on");
  });

  it("is partial when some are", () => {
    expect(tickOf(root, new Set([must(FILES[0]).path]))).toBe("partial");
  });

  it("is off for a directory with no files under it", () => {
    const empty: TreeNode = { name: "mcp", path: ".factorial/mcp", kind: "dir", children: [] };
    expect(tickOf(empty, new Set([must(FILES[0]).path]))).toBe("off");
  });

  it("keys a file node on its own path alone", () => {
    const leaf = childNamed(childNamed(childNamed(root, "agents"), "plan-agent"), "agent.yaml");
    expect(tickOf(leaf, new Set([leaf.path]))).toBe("on");
    expect(tickOf(leaf, new Set())).toBe("off");
  });
});

describe("pathsUnder", () => {
  it("lists every file path beneath a directory", () => {
    const root = must(buildTree(FILES)[0]);
    const agents = childNamed(root, "agents");
    expect(pathsUnder(agents)).toEqual([
      ".factorial/agents/plan-agent/agent.yaml",
      ".factorial/agents/plan-agent/system-prompt.md",
    ]);
  });

  it("returns just its own path for a file node", () => {
    const root = must(buildTree(FILES)[0]);
    const agentYaml = childNamed(
      childNamed(childNamed(root, "agents"), "plan-agent"),
      "agent.yaml",
    );
    expect(pathsUnder(agentYaml)).toEqual([".factorial/agents/plan-agent/agent.yaml"]);
  });
});

describe("mergeFiles", () => {
  it("gives a path present in both sources one node carrying both halves", () => {
    const path = ".factorial/agents/plan-agent/agent.yaml";
    const [merged, ...rest] = mergeFiles([repo(path)], [proposal(path)]);
    expect(rest).toHaveLength(0);
    expect(must(merged).repo).toBeDefined();
    expect(must(merged).proposal).toBeDefined();
  });

  it("keeps a path only one source has", () => {
    const inRepo = ".factorial/skills/project-constitution/SKILL.md";
    const proposed = ".factorial/agents/plan-agent/agent.yaml";
    const merged = mergeFiles([repo(inRepo)], [proposal(proposed)]);
    expect(merged.map((f) => f.path)).toEqual([proposed, inRepo].sort());
  });
});

describe("selectableUnder", () => {
  it("counts a proposed file and skips one the repo already has", () => {
    // `assemble.py` refuses any path the repo owns, so a tick must never be able to add one.
    const proposed = ".factorial/agents/plan-agent/agent.yaml";
    const owned = ".factorial/agents/plan-agent/system-prompt.md";
    const root = must(
      buildTree([
        { path: proposed, proposal: proposal(proposed) },
        { path: owned, repo: repo(owned) },
      ])[0],
    );
    expect(selectableUnder(root)).toEqual([proposed]);
    expect(pathsUnder(root)).toHaveLength(2);
  });

  it("leaves a directory of repo files off rather than for ever partial", () => {
    const owned = ".factorial/skills/project-constitution/SKILL.md";
    const root = must(buildTree([{ path: owned, repo: repo(owned) }])[0]);
    expect(tickOf(root, new Set([owned]))).toBe("off");
  });
});

describe("changeOf", () => {
  const NEW = ".factorial/agents/plan-agent/agent.yaml";
  const OWNED = ".factorial/process.yaml";
  const tree = () =>
    must(
      buildTree([
        { path: NEW, proposal: proposal(NEW) },
        { path: OWNED, repo: repo(OWNED, { kind: "process" }) },
      ])[0],
    );

  it("calls a ticked new file added", () => {
    const node = childNamed(childNamed(tree(), "agents"), "plan-agent");
    expect(changeOf(childNamed(node, "agent.yaml"), new Set([NEW]), new Set())).toBe("added");
  });

  it("says nothing about an unticked file, which the commit will not write", () => {
    const node = childNamed(childNamed(tree(), "agents"), "plan-agent");
    expect(changeOf(childNamed(node, "agent.yaml"), new Set(), new Set())).toBeNull();
  });

  it("calls a repo file the commit rewrites modified", () => {
    expect(changeOf(childNamed(tree(), "process.yaml"), new Set(), new Set([OWNED]))).toBe(
      "modified",
    );
  });

  it("says nothing about a repo file nothing touches", () => {
    expect(changeOf(childNamed(tree(), "process.yaml"), new Set([NEW]), new Set())).toBeNull();
  });

  it("calls a file the commit creates outside the ticks added: a new lane's own process file", () => {
    const LANE = ".factorial/processes/process-hotfix.yaml";
    const root = must(buildTree([{ path: LANE }])[0]);
    const lane = childNamed(childNamed(root, "processes"), "process-hotfix.yaml");
    expect(changeOf(lane, new Set(), new Set(), new Set([LANE]))).toBe("added");
    expect(changeOf(root, new Set(), new Set(), new Set([LANE]))).toBe("added");
  });

  it("gives a directory the strongest answer beneath it", () => {
    // A folder holding both a new file and a changed one is a folder something is changed in.
    expect(changeOf(tree(), new Set([NEW]), new Set())).toBe("added");
    expect(changeOf(tree(), new Set([NEW]), new Set([OWNED]))).toBe("modified");
    expect(changeOf(tree(), new Set(), new Set())).toBeNull();
  });
});
