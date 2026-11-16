import { describe, expect, it } from "vitest";
import {
  constructFiles,
  constructFolder,
  constructIcon,
  constructLabel,
  constructName,
  constructNameProblem,
  constructsFor,
  isEmptyManifest,
} from "./constructs";

const DIR = ".factorial";
const SHELLS = {
  agent: {
    "agent.yaml": "name: __NAME__\n",
    "system-prompt.md": "You are `__NAME__`.\n",
    "instructions.md": "Instructions for `__NAME__`.\n",
  },
  skill: { "SKILL.md": "---\nname: __NAME__\n---\n" },
  mcp: { "server.yaml": "name: __NAME__\n" },
  process: { "process.yaml": "name: __NAME__ # process-__NAME__.yaml\n" },
};

const labels = (folder: string) => constructsFor(folder, DIR).map(constructLabel);

describe("what a folder offers", () => {
  it("offers all four constructs on the config folder, in the order agent, MCP server, process, skill", () => {
    expect(labels(".factorial")).toEqual([
      "New agent",
      "New MCP server",
      "New process",
      "New skill",
    ]);
  });

  it("offers each top-level folder its own construct", () => {
    expect(labels(".factorial/agents")).toEqual(["New agent"]);
    expect(labels(".factorial/mcp")).toEqual(["New MCP server"]);
    expect(labels(".factorial/processes")).toEqual(["New process"]);
    expect(labels(".factorial/skills")).toEqual(["New skill"]);
    expect(labels(".factorial/skills/review-notes")).toEqual(["New skill"]);
  });

  it("offers one agent's own skill and MCP server inside that agent's folder", () => {
    expect(labels(".factorial/agents/plan-agent")).toEqual([
      "New MCP server for plan-agent",
      "New skill for plan-agent",
    ]);
    expect(labels(".factorial/agents/plan-agent/skills")).toEqual(["New skill for plan-agent"]);
    expect(labels(".factorial/agents/plan-agent/skills/notes")).toEqual([
      "New skill for plan-agent",
    ]);
    expect(labels(".factorial/agents/plan-agent/mcp")).toEqual(["New MCP server for plan-agent"]);
  });

  it("offers nothing on a folder no construct belongs in", () => {
    expect(labels(".factorial/notes")).toEqual([]);
    expect(labels(".github")).toEqual([]);
    expect(labels(".factorial/agents/plan-agent/reference")).toEqual([]);
  });

  it("names each in the menu by the thing alone, with an icon of its own", () => {
    const menu = constructsFor(".factorial", DIR);
    expect(menu.map(constructName)).toEqual(["Agent", "MCP server", "Process", "Skill"]);
    expect(menu.map(constructIcon)).toEqual(["agent", "plug", "lane", "file-markdown"]);
    // Whose it is shows in the folder that was pressed, and again in the prompt.
    expect(constructsFor(".factorial/agents/plan-agent", DIR).map(constructName)).toEqual([
      "MCP server",
      "Skill",
    ]);
  });

  it("says where each construct goes", () => {
    expect(constructFolder({ kind: "agent" }, DIR)).toBe(".factorial/agents");
    expect(constructFolder({ kind: "process" }, DIR)).toBe(".factorial/processes");
    expect(constructFolder({ kind: "skill", agent: "plan-agent" }, DIR)).toBe(
      ".factorial/agents/plan-agent/skills",
    );
    expect(constructFolder({ kind: "mcp", agent: "plan-agent" }, DIR)).toBe(
      ".factorial/agents/plan-agent/mcp",
    );
  });
});

describe("a construct's files", () => {
  it("puts each construct's files where the loader reads them, the manifest first", () => {
    expect(Object.keys(constructFiles(SHELLS, { kind: "agent" }, DIR, "review-agent"))).toEqual([
      ".factorial/agents/review-agent/agent.yaml",
      ".factorial/agents/review-agent/instructions.md",
      ".factorial/agents/review-agent/system-prompt.md",
    ]);
    expect(Object.keys(constructFiles(SHELLS, { kind: "skill" }, DIR, "notes"))).toEqual([
      ".factorial/skills/notes/SKILL.md",
    ]);
    expect(Object.keys(constructFiles(SHELLS, { kind: "mcp" }, DIR, "tracker"))).toEqual([
      ".factorial/mcp/tracker.yaml",
    ]);
    expect(Object.keys(constructFiles(SHELLS, { kind: "process" }, DIR, "fast"))).toEqual([
      ".factorial/processes/process-fast.yaml",
    ]);
    expect(
      Object.keys(constructFiles(SHELLS, { kind: "skill", agent: "plan-agent" }, DIR, "notes")),
    ).toEqual([".factorial/agents/plan-agent/skills/notes/SKILL.md"]);
    expect(
      Object.keys(constructFiles(SHELLS, { kind: "mcp", agent: "plan-agent" }, DIR, "tracker")),
    ).toEqual([".factorial/agents/plan-agent/mcp/tracker.yaml"]);
  });

  it("fills the name into every file, everywhere it appears", () => {
    const files = constructFiles(SHELLS, { kind: "process" }, DIR, "fast");
    expect(files[".factorial/processes/process-fast.yaml"]).toBe(
      "name: fast # process-fast.yaml\n",
    );
    const agent = constructFiles(SHELLS, { kind: "agent" }, DIR, "review-agent");
    expect(Object.values(agent).join("")).not.toContain("__NAME__");
    expect(agent[".factorial/agents/review-agent/system-prompt.md"]).toBe(
      "You are `review-agent`.\n",
    );
  });

  it("makes nothing from a kind the server sent no shell for", () => {
    expect(constructFiles({}, { kind: "agent" }, DIR, "review-agent")).toEqual({});
  });
});

describe("a construct's name", () => {
  const problem = (
    construct: Parameters<typeof constructNameProblem>[0],
    name: string,
    taken: string[] = [],
    proposed: string[] = [],
  ) => constructNameProblem(construct, name, DIR, taken, proposed);

  it("takes a legal, free name", () => {
    expect(problem({ kind: "agent" }, "review-agent")).toBeNull();
    expect(problem({ kind: "mcp" }, "tracker")).toBeNull();
    expect(problem({ kind: "process" }, "fast-2")).toBeNull();
    expect(problem({ kind: "skill", agent: "plan-agent" }, "Review_Notes")).toBeNull();
  });

  it("refuses a name that is not one safe segment", () => {
    for (const name of ["", "a/b", "..", ".hidden", "two words"]) {
      expect(problem({ kind: "agent" }, name)).toMatch(/starts with a letter or digit/);
    }
  });

  it("holds an MCP server to a tool prefix and a process to a slug", () => {
    expect(problem({ kind: "mcp" }, "Tracker")).toBe(
      "Use lowercase letters, digits and dashes. The name becomes the tool prefix.",
    );
    expect(problem({ kind: "process" }, "Fast")).toBe(
      "Use lowercase letters, digits and dashes, starting and ending with a letter or digit.",
    );
    expect(problem({ kind: "process" }, "fast-")).not.toBeNull();
    expect(problem({ kind: "process" }, "default")).toBe("The name default is reserved.");
  });

  it("refuses a name a file or a folder already uses", () => {
    expect(
      problem({ kind: "agent" }, "plan-agent", [".factorial/agents/plan-agent/notes.md"]),
    ).toBe("An agent with that name is already here.");
    expect(problem({ kind: "mcp" }, "tracker", [".factorial/mcp/tracker.yaml"])).toBe(
      "An MCP server with that name is already here.",
    );
    expect(problem({ kind: "process" }, "fast", [".factorial/processes/process-fast.yaml"])).toBe(
      "A process with that name is already here.",
    );
    expect(problem({ kind: "skill" }, "notes", [".factorial/skills/notes/SKILL.md"])).toBe(
      "A skill with that name is already here.",
    );
  });

  it("sends a catalog agent's name to its template", () => {
    expect(
      problem({ kind: "agent" }, "plan-agent", [], [".factorial/agents/plan-agent/agent.yaml"]),
    ).toBe("plan-agent has a template. Tick its files in the explorer.");
  });
});

describe("an empty manifest", () => {
  it("is one with no line that is neither blank nor a comment", () => {
    expect(isEmptyManifest("")).toBe(true);
    expect(isEmptyManifest("# Agent manifest. SCAFFOLDED, NOT FINISHED.\n\n  # name: x\n")).toBe(
      true,
    );
    expect(isEmptyManifest("# a comment\nname: x\n")).toBe(false);
  });
});
