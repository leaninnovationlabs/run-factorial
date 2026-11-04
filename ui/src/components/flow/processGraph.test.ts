import { describe, expect, it } from "vitest";
import { conditionCalls, isProcessConfigLike, processGraphFromConfig } from "./processGraph";

const factorialSimple = {
  entry: { step: "plan" },
  steps: [
    { name: "plan", kind: "agent" as const, next: "spec_gate" },
    {
      name: "spec_gate",
      kind: "human_gate" as const,
      next: "build",
      gate: { onReject: { rerunStep: "plan" } },
    },
    { name: "build", kind: "agent" as const, next: "test" },
    { name: "test", kind: "agent" as const, next: "code_gate" },
    {
      name: "code_gate",
      kind: "human_gate" as const,
      next: "done",
      gate: { onReject: { rerunStep: "build" } },
    },
  ],
  terminal: { step: "done" },
};

describe("processGraphFromConfig", () => {
  it("walks next pointers from entry to terminal and records reject loops", () => {
    const graph = processGraphFromConfig(factorialSimple);

    expect(graph.nodes.map((node) => node.id)).toEqual([
      "__entry",
      "plan",
      "spec_gate",
      "build",
      "test",
      "code_gate",
      "done",
    ]);
    expect(graph.edges).toEqual([
      { from: "__entry", to: "plan", kind: "next" },
      { from: "plan", to: "spec_gate", kind: "next" },
      { from: "spec_gate", to: "build", kind: "next" },
      { from: "spec_gate", to: "plan", kind: "reject" },
      { from: "build", to: "test", kind: "next" },
      { from: "test", to: "code_gate", kind: "next" },
      { from: "code_gate", to: "done", kind: "next" },
      { from: "code_gate", to: "build", kind: "reject" },
    ]);
  });

  it("keeps an action step's kind and ranks columns along next", () => {
    const graph = processGraphFromConfig({
      entry: { step: "intake" },
      steps: [
        { name: "intake", kind: "action", next: "review" },
        {
          name: "review",
          kind: "human_gate",
          next: "done",
          gate: { onReject: { rerunStep: "intake" } },
        },
      ],
      terminal: { step: "done" },
    });

    expect(graph.nodes.find((node) => node.id === "intake")?.kind).toBe("action");
    expect(graph.nodes.map((node) => [node.id, node.column])).toEqual([
      ["__entry", 0],
      ["intake", 1],
      ["review", 2],
      ["done", 3],
    ]);
  });

  it("keeps a check step's kind rather than defaulting it to agent", () => {
    const graph = processGraphFromConfig({
      entry: { step: "validate_ci" },
      steps: [{ name: "validate_ci", kind: "check", next: "done" }],
      terminal: { step: "done" },
    });

    expect(graph.nodes.find((node) => node.id === "validate_ci")?.kind).toBe("check");
  });

  it("reads snake_case snapshot keys the same as the YAML aliases", () => {
    const graph = processGraphFromConfig({
      entry: { step: "plan" },
      steps: [
        { name: "plan", kind: "agent", next: "spec_gate" },
        {
          name: "spec_gate",
          kind: "human_gate",
          next: "done",
          gate: { on_reject: { rerun_step: "plan" } },
        },
      ],
      terminal: { step: "done" },
    });

    expect(graph.edges).toContainEqual({ from: "spec_gate", to: "plan", kind: "reject" });
  });
});

describe("isProcessConfigLike", () => {
  it("flattens a condition tree to the calls config wrote", () => {
    expect(conditionCalls({ all: ['pr_merged("spec/{ticket}-*", "main")'] })).toEqual([
      'pr_merged("spec/{ticket}-*", "main")',
    ]);
    expect(
      conditionCalls({ all: ["ticket_has_assignee()", { any: ["a()", { not: "b()" }] }] }),
    ).toEqual(["ticket_has_assignee()", "a()", "b()"]);
  });

  it("reads the attribute spellings a hand-built snapshot may carry", () => {
    expect(conditionCalls({ all_of: ["a()", { not_: "b()" }] })).toEqual(["a()", "b()"]);
  });

  it("answers nothing for a step that declares no conditions", () => {
    expect(conditionCalls(undefined)).toEqual([]);
    expect(conditionCalls(null)).toEqual([]);
  });

  it("accepts a snapshot the mapper can walk", () => {
    expect(isProcessConfigLike(factorialSimple)).toBe(true);
  });

  it("rejects a snapshot missing any pointer the mapper reads", () => {
    expect(isProcessConfigLike(null)).toBe(false);
    expect(isProcessConfigLike({ specVersion: "adlc/v1" })).toBe(false);
    expect(isProcessConfigLike({ entry: { step: "plan" }, steps: [] })).toBe(false);
    expect(isProcessConfigLike({ entry: { step: "plan" }, terminal: { step: "done" } })).toBe(
      false,
    );
  });
});
