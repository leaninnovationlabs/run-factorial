// @vitest-environment happy-dom
/**
 * A lane's graph and its agent cards point at each other: a card selects its agent's step, and a
 * selected step marks its agent's card, so "what runs where" reads either way round.
 */

import { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";
import type { AgentDetail } from "../../api/types";
import { byText, click, type Mounted, mount } from "../../testDom";
import type { ProcessConfigLike } from "../flow";
import { LaneView } from "./LaneView";

const CONFIG = {
  entry: { step: "plan" },
  steps: [
    { name: "plan", kind: "agent", agent: "plan-agent", next: "build" },
    { name: "build", kind: "agent", agent: "build-agent", next: "done" },
  ],
  terminal: { step: "done" },
} as unknown as ProcessConfigLike;

const agent = (name: string): AgentDetail => ({
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
});

function Harness({ start = null }: { start?: string | null }) {
  const [selected, setSelected] = useState<string | null>(start);
  return (
    <>
      <output data-testid="selected">{selected ?? ""}</output>
      <LaneView
        config={CONFIG}
        agents={{ list: [agent("plan-agent"), agent("build-agent")], pending: false, error: null }}
        selectedId={selected}
        onSelect={setSelected}
      />
    </>
  );
}

let view: Mounted | null = null;
afterEach(async () => {
  await view?.unmount();
  view = null;
});

const selected = () => document.querySelector('[data-testid="selected"]')?.textContent;
const card = (name: string) =>
  [...document.querySelectorAll("button[aria-pressed]")].find((button) =>
    button.textContent?.startsWith(name),
  );

describe("LaneView", () => {
  it("selects an agent's first step when its card is pressed, and clears on a second press", async () => {
    view = await mount(<Harness />);
    await click(card("build-agent"));
    expect(selected()).toBe("build");
    expect(card("build-agent")?.getAttribute("aria-pressed")).toBe("true");
    await click(card("build-agent"));
    expect(selected()).toBe("");
  });

  it("marks the card of the agent whose step is selected on the graph", async () => {
    view = await mount(<Harness start="plan" />);
    expect(card("plan-agent")?.getAttribute("aria-pressed")).toBe("true");
    expect(card("build-agent")?.getAttribute("aria-pressed")).toBe("false");
    expect(byText(document.body, "h3", "plan")).not.toBeNull();
  });
});
