// @vitest-environment happy-dom
/**
 * What a lane's nodes run, drawn from whichever agent list the caller has: the synced version's,
 * or a gallery preview's. Mostly server-rendered, as `ProjectConfig.test.tsx` is; the query test
 * mounts, because what it is about is a live query's state.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useProcessVersionAgents } from "../../api/queries";
import type { AgentDetail } from "../../api/types";
import { click, type Mounted, mount } from "../../testDom";
import type { ProcessConfigLike } from "../flow";
import { AgentCards, agentDetails, agentsFromQuery } from "./AgentCards";
import { StepDetails } from "./StepDetails";

const CONFIG = {
  entry: { step: "plan" },
  steps: [{ name: "plan", kind: "agent", agent: "plan-agent", next: "done" }],
  terminal: { step: "done" },
} as unknown as ProcessConfigLike;

const PLAN: AgentDetail = {
  name: "plan-agent",
  warning: null,
  model: "claude-haiku-4-5-20251001",
  max_turns: 40,
  skills_local: [],
  skills_shared: ["project-constitution"],
  mcp_local: [],
  mcp_shared: [],
  secrets: ["GH_TOKEN"],
  permissions: null,
};

const ready = { list: [PLAN], pending: false, error: null };

describe("AgentCards", () => {
  it("draws one pressable card per agent, with its steps, model and limits", () => {
    const html = renderToString(
      <AgentCards config={CONFIG} agents={ready} selectedAgent="plan-agent" onPick={() => {}} />,
    );
    expect(html).toContain("plan-agent");
    expect(html).toContain("runs plan");
    expect(html).toContain("claude-haiku-4-5-20251001");
    expect(html).toContain("40 turns");
    expect(html).not.toContain("· 1h");
    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain('data-glyph="agent"');
  });

  it("draws the skills and the MCP servers as groups of their own, saying when one is empty", () => {
    const html = renderToString(
      <AgentCards config={CONFIG} agents={ready} selectedAgent={null} onPick={() => {}} />,
    );
    const groups = [...html.matchAll(/<h3[^>]*>([^<]+)<\/h3>/g)].map((match) => match[1]);
    expect(groups).toEqual(["Agents", "Skills", "MCP servers"]);
    expect(html).toContain('data-tool-card="skill:shared:project-constitution"');
    expect(html).toContain("shared · 1 agent");
    expect(html).toContain("No agent in this process uses an MCP server.");
  });

  it("puts an agent's whole manifest in brief behind its card", () => {
    expect(agentDetails({ ...PLAN, mcp_shared: ["docs"] }, ["plan"]).split("\n")).toEqual([
      "plan-agent",
      "Runs: plan",
      "Model: claude-haiku-4-5-20251001",
      "Limits: 40 turns",
      "Skills: project-constitution",
      "MCP servers: docs",
      "Secrets: GH_TOKEN",
      "Permissions: the default profile",
    ]);
  });

  it("shows a manifest's warning instead of its facts", () => {
    const html = renderToString(
      <AgentCards
        config={CONFIG}
        agents={{
          list: [{ ...PLAN, warning: "empty document", model: null }],
          pending: false,
          error: null,
        }}
        selectedAgent={null}
        onPick={() => {}}
      />,
    );
    expect(html).toContain("empty document");
    expect(html).not.toContain("40 turns");
  });
});

describe("AgentCards, when the list is not there", () => {
  let view: Mounted | null = null;
  afterEach(async () => {
    await view?.unmount();
    view = null;
  });

  /** A lane with no synced version yet: the agents query has nothing to read and never runs. */
  function NeverSynced() {
    const agents = useProcessVersionAgents(1, undefined, true);
    return (
      <AgentCards
        config={CONFIG}
        agents={agentsFromQuery(agents)}
        selectedAgent={null}
        onPick={() => {}}
      />
    );
  }

  it("does not say Reading for an agents query that will never run", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    view = await mount(
      <QueryClientProvider client={qc}>
        <NeverSynced />
      </QueryClientProvider>,
    );
    expect(document.body.textContent).toContain("plan-agent");
    expect(document.body.textContent).not.toContain("Loading");
  });

  it("does not call an agent missing from the config when the read failed", () => {
    const html = renderToString(
      <AgentCards
        config={CONFIG}
        agents={{ list: undefined, pending: false, error: new Error("boom") }}
        selectedAgent={null}
        onPick={() => {}}
      />,
    );
    expect(html).toContain("boom");
    expect(html).not.toContain("not in the synced config");
  });
});

describe("AgentCards' chips", () => {
  let view: Mounted | null = null;
  afterEach(async () => {
    await view?.unmount();
    view = null;
  });

  it("open their file when there is somewhere to open it", async () => {
    const onOpenTool = vi.fn();
    view = await mount(
      <AgentCards
        config={CONFIG}
        agents={ready}
        selectedAgent={null}
        onPick={() => {}}
        onOpenTool={onOpenTool}
      />,
    );
    const chip = [...document.querySelectorAll("button")].find((button) =>
      button.textContent?.startsWith("project-constitution"),
    );
    await click(chip);
    expect(onOpenTool).toHaveBeenCalledWith("skills/project-constitution/SKILL.md");
  });

  it("stay plain labels in a preview, which has no editor to open", () => {
    const html = renderToString(
      <AgentCards config={CONFIG} agents={ready} selectedAgent={null} onPick={() => {}} />,
    );
    expect(html).toContain("project-constitution");
    expect(html).not.toMatch(/<button[^>]*>(?:<[^>]+>)*project-constitution/);
  });
});

describe("StepDetails", () => {
  it("reads an agent step's rows from the list it is handed", () => {
    const html = renderToString(<StepDetails agents={ready} config={CONFIG} nodeId="plan" />);
    expect(html).toContain("claude-haiku-4-5-20251001");
    expect(html).toContain("GH_TOKEN");
  });
});
