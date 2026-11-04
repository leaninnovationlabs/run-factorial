/**
 * Where the panel says a run's session lives, per substrate, and what its Copy button copies. The
 * panel itself renders through a portal, which `renderToString` refuses, so these are tested alone.
 */

import { describe, expect, it } from "vitest";
import type { ActivityTurn } from "../../api/types";
import { sessionReference, transcriptText } from "./Transcript";

const RUN_ID = "157-plan-agent-ef368a05";

describe("sessionReference", () => {
  it("names the codex run's transcript file under its local root", () => {
    expect(sessionReference("codex_cli", RUN_ID)).toBe(`<local_root>/runs/${RUN_ID}/stdout.jsonl`);
  });

  it("names the claude run's directory under its local root", () => {
    expect(sessionReference("claude_cli", RUN_ID)).toBe(`<local_root>/runs/${RUN_ID}/`);
  });

  it.each(["claude_e2b", "codex_e2b"])(
    "names the %s run's mirror under the E2B state root",
    (kind) => {
      expect(sessionReference(kind, RUN_ID)).toBe(`<e2b state_root>/runs/${RUN_ID}/stdout.jsonl`);
    },
  );

  it("claims nothing for an unrecorded substrate", () => {
    expect(sessionReference(undefined, RUN_ID)).toBeNull();
    expect(sessionReference("claude_managed", RUN_ID)).toBeNull();
  });
});

describe("transcriptText", () => {
  it("copies what was said and what was run, turn by turn, with each call's outcome", () => {
    const turns = [
      {
        index: 1,
        text: ["Reading the spec."],
        tools: [{ tool: "Read", target: "spec.md", ok: true }],
        thinking: [],
      },
      {
        index: 2,
        text: [],
        tools: [{ tool: "Bash", target: "pytest", ok: false }],
        thinking: ["kept out of the copy"],
      },
      {
        index: 3,
        text: [],
        tools: [
          { tool: "AskUserQuestion", target: null, ok: true, question: "Tabs?", answer: "Spaces" },
          { tool: "AskUserQuestion", target: null, ok: null, question: "Ship?", answer: null },
        ],
        thinking: [],
      },
    ] as unknown as ActivityTurn[];
    expect(transcriptText(turns, ["cloned"]).split("\n")).toEqual([
      "# cloned",
      "1 > Reading the spec.",
      "1 [ok] Read spec.md",
      "2 [failed] Bash pytest",
      "3 [ok] AskUserQuestion",
      "3 Q: Tabs? A: Spaces",
      "3 [running] AskUserQuestion",
      "3 Q: Ship? A: (no answer)",
    ]);
  });
});
