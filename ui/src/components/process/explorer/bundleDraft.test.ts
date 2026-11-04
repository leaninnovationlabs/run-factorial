/**
 * `parseDraft` reads a value nobody validated on the way in — same contract as `prefs.ts`'s
 * `parsePrefs` — so what is worth pinning is that it is TOTAL: every input produces a draft the
 * explorer can render, and a shape it cannot read costs the reader that one file rather than the
 * page. `readBundleDraft` / `writeBundleDraft` are one line each over `storage.ts`, tested there.
 *
 * `reconcile` is the load-bearing rule this module exists for, and gets full coverage below.
 */

import { describe, expect, it } from "vitest";
import type { FileProposal } from "../../../api/types";
import { type DraftFile, key, parseDraft, reconcile, serializeDraft } from "./bundleDraft";

const proposal = (path: string, body: string, facts: Record<string, string>): FileProposal => ({
  path,
  body,
  component: "plan-agent",
  kind: "agent",
  variant: "base-target",
  version: 1,
  facts,
  present_in_repo: false,
});

const PATH = ".factorial/agents/plan-agent/instructions.md";

describe("parseDraft", () => {
  it("round-trips what the explorer actually writes", () => {
    const draft: Record<string, DraftFile> = {
      [PATH]: { body: "x", facts: { __BRANCH_PREFIX__: "spec" } },
    };
    expect(parseDraft(serializeDraft(draft))).toEqual(draft);
  });

  it("is null when nothing is stored", () => {
    expect(parseDraft(null)).toBeNull();
  });

  it("falls back to null for anything that is not a stored draft at all", () => {
    for (const raw of ["not json", "[]", '"a string"', "7", "null"]) {
      expect(parseDraft(raw)).toBeNull();
    }
  });

  it("keeps the files it can read when one entry is unusable", () => {
    // Field by field, the way `parsePrefs` treats a bad threshold: an entry a future or hand-
    // edited version wrote wrong should cost the reader that one file, not every edit they made.
    const stored = JSON.stringify({
      [PATH]: { body: "mine", facts: {} },
      ".factorial/agents/plan-agent/agent.yaml": { body: 7 }, // not a string body
      ".factorial/agents/plan-agent/system-prompt.md": "just a string",
    });
    expect(parseDraft(stored)).toEqual({ [PATH]: { body: "mine", facts: {} } });
  });

  it("defaults facts to empty rather than rejecting the file over them", () => {
    const stored = JSON.stringify({ [PATH]: { body: "mine" } });
    expect(parseDraft(stored)?.[PATH]?.facts).toEqual({});
  });

  it("drops a fact value that is not itself a string", () => {
    const stored = JSON.stringify({
      [PATH]: { body: "mine", facts: { __BRANCH_PREFIX__: "spec", __MAX_ATTEMPTS__: 3 } },
    });
    expect(parseDraft(stored)?.[PATH]?.facts).toEqual({ __BRANCH_PREFIX__: "spec" });
  });
});

describe("reconcile", () => {
  it("silently replaces a body the operator never edited", () => {
    const draft = { [PATH]: { body: "old", facts: { __BRANCH_PREFIX__: "spec" } } };
    const next = [proposal(PATH, "new", { __BRANCH_PREFIX__: "spec" })];
    const result = reconcile(draft, next, { [PATH]: "old" });
    expect(result.draft[PATH]?.body).toBe("new");
    expect(result.stale).toEqual([]);
  });

  it("keeps an edited body and marks it stale when a fact it used changed", () => {
    const draft = { [PATH]: { body: "mine", facts: { __BRANCH_PREFIX__: "spec" } } };
    const next = [proposal(PATH, "new", { __BRANCH_PREFIX__: "release" })];
    const result = reconcile(draft, next, { [PATH]: "old" });
    expect(result.draft[PATH]?.body).toBe("mine");
    expect(result.stale).toEqual([PATH]);
  });

  it("does not mark an edited body stale when an unrelated fact changed", () => {
    const draft = { [PATH]: { body: "mine", facts: { __BRANCH_PREFIX__: "spec" } } };
    const next = [proposal(PATH, "new", { __BRANCH_PREFIX__: "spec", __SPECS_DIR__: "plans" })];
    const result = reconcile(draft, next, { [PATH]: "old" });
    expect(result.stale).toEqual([]);
  });

  it("drops a path the new proposal no longer contains", () => {
    const draft = { [PATH]: { body: "mine", facts: {} } };
    expect(reconcile(draft, [], {}).draft).toEqual({});
  });

  it("only compares facts the edited file itself held, not every fact in the new proposal", () => {
    // A file that used no markers at all cannot go stale over one appearing for the first time —
    // it never read it, the way `instructions.md` never reads a marker only `system-prompt.md`
    // names.
    const draft = { [PATH]: { body: "mine", facts: {} } };
    const next = [proposal(PATH, "new", { __BRANCH_PREFIX__: "release" })];
    const result = reconcile(draft, next, { [PATH]: "old" });
    expect(result.stale).toEqual([]);
  });
});

describe("key", () => {
  it("separates two lanes of one project, matching processDraft's key", () => {
    // Same reason: one key for every lane restores one lane's file edits into another.
    expect(key(9, "standard")).not.toEqual(key(9, "hotfix"));
    expect(key(9, null)).not.toEqual(key(9, "standard"));
  });
});
