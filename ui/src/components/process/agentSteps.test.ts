import { describe, expect, it } from "vitest";
import type { AgentDetail } from "../../api/types";
import type { ProcessConfigLike } from "../flow";
import { agentOfStep, laneCounts, stepsByAgent, toolChips, toolPath } from "./agentSteps";

const CONFIG = {
  entry: { step: "plan" },
  steps: [
    { name: "plan", kind: "agent", agent: "plan-agent", next: "gate" },
    { name: "gate", kind: "human_gate", next: "build" },
    { name: "build", kind: "agent", agent: "build-agent", next: "fix" },
    { name: "fix", kind: "agent", agent: "build-agent", next: "done" },
  ],
  terminal: { step: "done" },
} as unknown as ProcessConfigLike;

const agent = (name: string, overrides: Partial<AgentDetail> = {}): AgentDetail => ({
  name,
  warning: null,
  model: "claude-haiku-4-5-20251001",
  max_turns: 40,
  skills_local: [],
  skills_shared: [],
  mcp_local: [],
  mcp_shared: [],
  secrets: [],
  permissions: null,
  ...overrides,
});

describe("agentSteps", () => {
  it("lists each agent with the steps it runs, in step order", () => {
    expect([...stepsByAgent(CONFIG)]).toEqual([
      ["plan-agent", ["plan"]],
      ["build-agent", ["build", "fix"]],
    ]);
  });

  it("names the agent a step runs, and none for a gate or nothing", () => {
    expect(agentOfStep(CONFIG, "fix")).toBe("build-agent");
    expect(agentOfStep(CONFIG, "gate")).toBeNull();
    expect(agentOfStep(CONFIG, null)).toBeNull();
  });

  it("counts steps, distinct agents and gates", () => {
    expect(laneCounts(CONFIG)).toEqual({ steps: 4, agents: 2, gates: 1 });
  });

  it("joins a shared skill across agents but keeps a local one to its agent", () => {
    const chips = toolChips([
      agent("plan-agent", { skills_shared: ["constitution"], skills_local: ["notes"] }),
      agent("build-agent", {
        skills_shared: ["constitution"],
        skills_local: ["notes"],
        mcp_shared: ["docs"],
      }),
    ]);
    expect(chips.map(({ name, kind, scope, agents }) => [name, kind, scope, agents])).toEqual([
      ["docs", "mcp", "shared", ["build-agent"]],
      ["constitution", "skill", "shared", ["plan-agent", "build-agent"]],
      ["notes", "skill", "local", ["build-agent"]],
      ["notes", "skill", "local", ["plan-agent"]],
    ]);
  });
});

describe("toolPath", () => {
  it("finds each chip's file under the config folder: shared at the top, local under its agent", () => {
    const paths = toolChips([
      agent("plan-agent", {
        skills_shared: ["constitution"],
        skills_local: ["notes"],
        mcp_shared: ["docs"],
        mcp_local: ["tracker"],
      }),
    ]).map(toolPath);
    expect(paths).toEqual([
      "mcp/docs.yaml",
      "agents/plan-agent/mcp/tracker.yaml",
      "skills/constitution/SKILL.md",
      "agents/plan-agent/skills/notes/SKILL.md",
    ]);
  });
});
