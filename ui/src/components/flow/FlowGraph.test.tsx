import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { FlowGraph, nodeName } from "./FlowGraph";
import type { ProcessGraph } from "./processGraph";

const GRAPH: ProcessGraph = {
  nodes: [
    { id: "plan", label: "plan", kind: "agent", column: 0 },
    { id: "review", label: "review", kind: "human_gate", column: 1 },
    { id: "done", label: "done", kind: "terminal", column: 2 },
  ],
  edges: [
    { from: "plan", to: "review", kind: "next" },
    { from: "review", to: "done", kind: "next" },
  ],
} as ProcessGraph;

describe("nodeName", () => {
  it("says in words what the drawing says in colour, size and place", () => {
    expect(nodeName("implement", { tone: "error", badge: "2/2", current: true })).toBe(
      "implement: failed, attempt 2 of 2, current step",
    );
    expect(nodeName("review", { tone: "attention", badge: "1" })).toBe(
      "review: needs attention, 1 attempt",
    );
    expect(nodeName("ship", { muted: true })).toBe("ship: not reached");
    expect(nodeName("plan", {})).toBe("plan");
  });
});

describe("FlowGraph", () => {
  const graph = (onSelect?: (id: string | null) => void) =>
    renderToString(
      <FlowGraph
        graph={GRAPH}
        onSelect={onSelect}
        currentId="plan"
        visitedIds={new Set(["plan"])}
        nodeTones={{ plan: "error" }}
        badges={{ plan: "2" }}
      />,
    );

  it("is a group of step buttons when steps can be picked, never a listbox without its keys", () => {
    const html = graph(() => {});
    expect(html).toContain('role="group" aria-label="Process steps"');
    expect(html).not.toContain('role="listbox"');
    expect(html).toContain('aria-label="plan: failed, 2 attempts, current step"');
  });

  it("is one picture of the process when nothing can be picked", () => {
    expect(graph()).toContain('role="img" aria-label="plan, then review, then done"');
  });

  it("marks a failed step with a shape, and does not make it flash", () => {
    const html = graph(() => {});
    expect(html).toContain("flow-node-mark--error");
    expect(html).not.toMatch(/flow-node--current(?!-)/);
  });

  it("keeps the current step beating only while it runs", () => {
    const at = (currentRunning: boolean, nodeTones = {}) =>
      renderToString(
        <FlowGraph
          graph={GRAPH}
          currentId="plan"
          currentRunning={currentRunning}
          visitedIds={new Set(["plan"])}
          nodeTones={nodeTones}
        />,
      );
    expect(at(true)).toContain("flow-node--running");
    expect(at(false)).toMatch(/flow-node--current(?!-)/);
    expect(at(false)).not.toContain("flow-node--running");
    // A failure never flashes, running or not.
    expect(at(true, { plan: "error" })).not.toContain("flow-node--running");
  });
});
