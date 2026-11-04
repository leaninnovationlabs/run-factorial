import type { AgentDetail } from "../../api/types";
import type { ProcessConfigLike } from "../flow";

// A snapshot is `dict[str, Any]` on the wire; `agent` is read only from agent steps.
type StepWithAgent = { name: string; kind: string; agent?: unknown };

const stepsOf = (config: ProcessConfigLike) => config.steps as unknown as StepWithAgent[];

export function stepsByAgent(config: ProcessConfigLike): Map<string, string[]> {
  const out = new Map<string, string[]>();
  for (const step of stepsOf(config)) {
    if (step.kind !== "agent" || typeof step.agent !== "string") continue;
    out.set(step.agent, [...(out.get(step.agent) ?? []), step.name]);
  }
  return out;
}

export function agentOfStep(config: ProcessConfigLike, step: string | null): string | null {
  if (step === null) return null;
  const found = stepsOf(config).find((candidate) => candidate.name === step);
  return found?.kind === "agent" && typeof found.agent === "string" ? found.agent : null;
}

export function laneCounts(config: ProcessConfigLike) {
  return {
    steps: config.steps.length,
    agents: stepsByAgent(config).size,
    gates: config.steps.filter((step) => step.kind === "human_gate").length,
  };
}

export interface ToolChip {
  key: string;
  name: string;
  kind: "skill" | "mcp";
  scope: "local" | "shared";
  agents: string[];
}

/** A shared skill or server is one thing however many agents declare it; two agents' local
 *  skills of one name are two different files, so each keeps its own chip. */
export function toolChips(agents: AgentDetail[]): ToolChip[] {
  const chips = new Map<string, ToolChip>();
  const add = (kind: ToolChip["kind"], scope: ToolChip["scope"], name: string, agent: string) => {
    const key = scope === "shared" ? `${kind}:shared:${name}` : `${kind}:local:${agent}:${name}`;
    const chip = chips.get(key) ?? { key, name, kind, scope, agents: [] };
    chip.agents.push(agent);
    chips.set(key, chip);
  };
  for (const agent of agents) {
    for (const name of agent.mcp_shared) add("mcp", "shared", name, agent.name);
    for (const name of agent.mcp_local) add("mcp", "local", name, agent.name);
    for (const name of agent.skills_shared) add("skill", "shared", name, agent.name);
    for (const name of agent.skills_local) add("skill", "local", name, agent.name);
  }
  const rank = (chip: ToolChip) =>
    (chip.kind === "mcp" ? 0 : 2) + (chip.scope === "shared" ? 0 : 1);
  return [...chips.values()].sort(
    (a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name) || a.key.localeCompare(b.key),
  );
}

/** Where a chip's file sits under the config folder: a shared one at the folder's top, an agent's
 *  own under that agent. */
export function toolPath(chip: ToolChip): string {
  const owner = chip.scope === "local" ? `agents/${chip.agents[0]}/` : "";
  return chip.kind === "skill"
    ? `${owner}skills/${chip.name}/SKILL.md`
    : `${owner}mcp/${chip.name}.yaml`;
}
