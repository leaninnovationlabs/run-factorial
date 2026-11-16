import { describe, expect, it } from "vitest";
import type { Runtime } from "../../api/types";
import {
  commitButtonState,
  stubbedManifests,
  unconfirmedStaleCount,
  unpermitted,
} from "./commitGate";

const READY: Parameters<typeof commitButtonState>[0] = {
  loading: false,
  hasText: true,
  checkingConfig: false,
  checkFailed: false,
  checkingRepo: false,
  committing: false,
  unchanged: false,
  validForText: true,
  previewed: true,
  unconfirmedStale: 0,
};

/**
 * The button never goes dark without a reason: every state that stops it names why in `reason`,
 * which the page writes beside the button (a disabled button shows no tooltip, #167).
 */
describe("commitButtonState", () => {
  it("says the config is loading before anything else", () => {
    // No text yet because none has been read: asking for a config would be the wrong advice.
    expect(commitButtonState({ ...READY, loading: true, hasText: false }).reason).toBe(
      "Loading the config…",
    );
  });

  it("says a check that failed has failed, rather than checking for ever", () => {
    // The request answered with an error: nothing is running, and nothing will until an edit.
    expect(commitButtonState({ ...READY, checkFailed: true, validForText: false }).reason).toBe(
      "Factorial couldn't check the config. Edit it to check again.",
    );
  });

  it("allows a commit once text is valid, previewed, and nothing is stale", () => {
    expect(commitButtonState(READY)).toEqual({ reason: null, isLoading: false });
  });

  it("asks for a config when there is none", () => {
    expect(commitButtonState({ ...READY, hasText: false }).reason).toBe(
      "Write or import a config first.",
    );
  });

  it("says it is checking the config while the verdict is out", () => {
    expect(commitButtonState({ ...READY, checkingConfig: true, validForText: false }).reason).toBe(
      "Checking the config…",
    );
  });

  it("says it is checking the repo while the file list is out", () => {
    expect(commitButtonState({ ...READY, checkingRepo: true }).reason).toBe(
      "Checking which files the commit would write…",
    );
  });

  it("shows the request as loading while the commit is out, not as a reason", () => {
    expect(commitButtonState({ ...READY, committing: true })).toEqual({
      reason: null,
      isLoading: true,
    });
  });

  it("says there is nothing to commit when nothing differs from the pin", () => {
    expect(commitButtonState({ ...READY, unchanged: true }).reason).toBe(
      "Nothing to commit. Edit a file first.",
    );
  });

  it("asks for a config before saying there is nothing to commit", () => {
    expect(commitButtonState({ ...READY, hasText: false, unchanged: true }).reason).toBe(
      "Write or import a config first.",
    );
  });

  it("stops on an invalid config, and points at its errors", () => {
    expect(commitButtonState({ ...READY, validForText: false }).reason).toBe(
      "Fix the errors below first.",
    );
  });

  it("stops on an unconfirmed stale file alone, and names the count", () => {
    expect(commitButtonState({ ...READY, unconfirmedStale: 1 }).reason).toBe(
      "1 file changed since you edited it. Review it before you commit.",
    );
  });

  it("pluralizes the stale count", () => {
    expect(commitButtonState({ ...READY, unconfirmedStale: 2 }).reason).toBe(
      "2 files changed since you edited them. Review them before you commit.",
    );
  });

  it("stays shut until a preview has said which files the commit would write", () => {
    // A submission needs a server-computed file set, and there is none before a preview.
    expect(commitButtonState({ ...READY, previewed: false }).reason).toBe(
      "Checking which files the commit would write…",
    );
  });
});

describe("unconfirmedStaleCount", () => {
  it("is zero when nothing is stale", () => {
    expect(unconfirmedStaleCount([], new Set(), new Set())).toBe(0);
  });

  it("does not count a stale file the operator did not select", () => {
    expect(unconfirmedStaleCount(["a"], new Set(), new Set())).toBe(0);
  });

  it("does not count a stale file already confirmed", () => {
    expect(unconfirmedStaleCount(["a"], new Set(["a"]), new Set(["a"]))).toBe(0);
  });

  it("counts a stale, selected, unconfirmed file", () => {
    expect(unconfirmedStaleCount(["a"], new Set(["a"]), new Set())).toBe(1);
  });

  it("counts each qualifying path once, ignoring the rest", () => {
    expect(unconfirmedStaleCount(["a", "b", "c"], new Set(["a", "b"]), new Set(["b"]))).toBe(1);
  });
});

describe("stubbedManifests", () => {
  it("names each written agent manifest the request did not carry", () => {
    // The server writes a comments-only stub over exactly those, so they are the unfinished ones.
    const written = [
      ".factorial/process.yaml",
      ".factorial/agents/planner/agent.yaml",
      ".factorial/agents/planner/prompt.md",
      ".factorial/agents/coder/agent.yaml",
    ];
    const sent = [{ path: ".factorial/agents/coder/agent.yaml", body: "kind: Agent" }];
    expect(stubbedManifests(written, sent)).toEqual([".factorial/agents/planner/agent.yaml"]);
  });
});

describe("unpermitted", () => {
  const runtime = (slug: string, isolation?: string) =>
    ({ slug, details: isolation ? { isolation } : {} }) as Runtime;
  const rows = [runtime("codex-api-on-e2b", "e2b"), runtime("claude-cli-host")];

  it("returns an E2B slug the allowlist does not name", () => {
    expect(unpermitted(["codex-api-on-e2b"], rows, [])).toEqual(["codex-api-on-e2b"]);
  });

  it("never returns a host slug, a permitted slug or one it does not know", () => {
    expect(unpermitted(["claude-cli-host", "gone"], rows, [])).toEqual([]);
    expect(unpermitted(["codex-api-on-e2b"], rows, ["codex-api-on-e2b"])).toEqual([]);
  });
});
