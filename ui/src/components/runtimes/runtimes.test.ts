/**
 * The Runtime page's vocabulary, as computations: which family a type is, what to call it, when a
 * runtime counts as connected, and the slug the form proposes from a name.
 *
 * Each is checked against the server's own rule where one exists — `SLUG_RE` and
 * `NOT_IMPLEMENTED_TYPES` in `orchestrator/factorial/runtimes.py` — because a form that accepts
 * what the API refuses just moves the error to a worse place.
 */

import { describe, expect, it } from "vitest";
import {
  bedrockDefaults,
  bedrockModelOf,
  bedrockPartitionOf,
  bedrockRegionOf,
  credentialFormFor,
  credentialNameFor,
  credentialState,
  isolationOf,
  METADATA_KEY_RE,
  metadataProblem,
  RUNTIME_TYPE_LABELS,
  RUNTIME_TYPES,
  requiresStoredCredential,
  SLUG_RE,
  SUBSCRIPTION_CREDENTIAL,
  sandboxLabelOf,
  suggestSlug,
  typeAvailable,
} from "./runtimes";

describe("suggestSlug", () => {
  it("lowercases, folds runs of anything else into one dash, and trims the ends", () => {
    expect(suggestSlug("Codex Sub (team)")).toBe("codex-sub-team");
    expect(suggestSlug("  --Claude__API--  ")).toBe("claude-api");
    expect(suggestSlug("a")).toBe("a");
  });

  it("strips accents rather than dropping the letter under them", () => {
    expect(suggestSlug("Équipe Ünïcode")).toBe("equipe-unicode");
  });

  it("caps at the server's 63 characters, never ending on a dash", () => {
    const exact = `${"a".repeat(61)}-b`;
    expect(suggestSlug(exact)).toBe(exact);
    // One longer, and the cut would land on the dash: it goes too, rather than a slug ending
    // in one that SLUG_RE would take but a reader would see as truncated.
    expect(suggestSlug(`${"a".repeat(62)}-bcd`)).toBe("a".repeat(62));
  });

  it("proposes only what SLUG_RE accepts, or nothing", () => {
    for (const name of ["Codex Sub (team)", "Équipe Ünïcode", "x".repeat(200)]) {
      expect(suggestSlug(name)).toMatch(SLUG_RE);
    }
    expect(suggestSlug("!!!")).toBe("");
  });
});

describe("SLUG_RE", () => {
  it("is the registry's rule: lowercase, digits, dashes, no leading dash, at most 63", () => {
    expect(SLUG_RE.test("codex-sub")).toBe(true);
    expect(SLUG_RE.test("9lives")).toBe(true);
    expect(SLUG_RE.test("-codex")).toBe(false);
    expect(SLUG_RE.test("Codex")).toBe(false);
    expect(SLUG_RE.test("a".repeat(64))).toBe(false);
    expect(SLUG_RE.test("")).toBe(false);
  });
});

describe("types", () => {
  it("names every type the enum has, and no other", () => {
    expect(RUNTIME_TYPES).toEqual([
      "codex_api",
      "codex_subscription",
      "codex_bedrock",
      "claude_api",
      "claude_subscription",
    ]);
    for (const type of RUNTIME_TYPES) expect(RUNTIME_TYPE_LABELS[type]).toBeTruthy();
  });

  it("offers every type: the server refuses none with 501 any more", () => {
    for (const type of RUNTIME_TYPES) expect(typeAvailable(type)).toBe(true);
  });
});

describe("credentialNameFor", () => {
  it("names the one credential each type holds, as the API's table does", () => {
    // `orchestrator.factorial.runtimes.credential_name_for` (tech-stack decision 36). A test
    // cannot read the Python table from here, so the five literals are asserted on both sides.
    expect(credentialNameFor("codex_api")).toBe("OPENAI_API_KEY");
    expect(credentialNameFor("codex_subscription")).toBe("CODEX_AUTH_JSON");
    expect(credentialNameFor("codex_bedrock")).toBe("AWS_BEARER_TOKEN_BEDROCK");
    expect(credentialNameFor("claude_api")).toBe("ANTHROPIC_API_KEY");
    expect(credentialNameFor("claude_subscription")).toBe("CLAUDE_CODE_OAUTH_TOKEN");
    expect(SUBSCRIPTION_CREDENTIAL).toBe("CODEX_AUTH_JSON");
  });
});

describe("credentialFormFor", () => {
  it("gives every pasted credential a form and the device-login bundle none", () => {
    // The codex bundle comes in by device login or a pasted auth.json, never a one-line field.
    expect(credentialFormFor("codex_subscription")).toBeNull();
    for (const type of ["codex_api", "claude_api", "claude_subscription"] as const) {
      const form = credentialFormFor(type);
      expect(form).not.toBeNull();
      expect(form?.label).toBeTruthy();
      expect(form?.placeholder).toBeTruthy();
      expect(form?.hint).toBeTruthy();
    }
  });

  it("says what each one is and, for the token, how to mint it", () => {
    expect(credentialFormFor("codex_api")?.label).toBe("OpenAI API key");
    expect(credentialFormFor("claude_api")?.label).toBe("Anthropic API key");
    expect(credentialFormFor("claude_subscription")?.label).toBe("Claude Code OAuth token");
    expect(credentialFormFor("claude_subscription")?.hint).toContain("claude setup-token");
    expect(credentialFormFor("codex_api")?.noun).toBe("key");
    expect(credentialFormFor("claude_subscription")?.noun).toBe("token");
  });
});

describe("credentialState", () => {
  it("counts a subscription as connected only by its bundle, not by any stray name", () => {
    expect(credentialState("codex_subscription", [])).toBe("none");
    expect(credentialState("codex_subscription", ["OPENAI_API_KEY"])).toBe("none");
    expect(credentialState("codex_subscription", ["CODEX_AUTH_JSON"])).toBe("connected");
    expect(credentialState("claude_subscription", ["CODEX_AUTH_JSON"])).toBe("none");
    expect(credentialState("claude_subscription", ["CLAUDE_CODE_OAUTH_TOKEN"])).toBe("connected");
  });

  it("counts an api-key runtime as connected only by its own key, exactly as named", () => {
    expect(credentialState("codex_api", [])).toBe("none");
    expect(credentialState("codex_api", ["OTHER"])).toBe("none");
    expect(credentialState("codex_api", ["ANTHROPIC_API_KEY"])).toBe("none");
    expect(credentialState("codex_api", ["OTHER", "OPENAI_API_KEY"])).toBe("connected");
    expect(credentialState("claude_api", ["ANTHROPIC_API_KEY"])).toBe("connected");
  });
});

describe("isolation strategies", () => {
  it("names a runtime's sandbox by whose key a cloud sandbox runs on", () => {
    expect(sandboxLabelOf({ details: {}, sandbox_key: "own" })).toBe("On the host");
    expect(sandboxLabelOf({ details: { isolation: "e2b" }, sandbox_key: "own" })).toBe(
      "E2B sandbox",
    );
    // "Managed cloud sandbox" named both, and read as Factorial's.
    expect(sandboxLabelOf({ details: { isolation: "e2b" }, sandbox_key: "managed" })).toBe(
      "Factorial-managed sandbox",
    );
  });

  it("reads a row's isolation, and a row from before the field as the host", () => {
    expect(isolationOf({ isolation: "e2b" })).toBe("e2b");
    expect(isolationOf({})).toBe("local_cli");
    expect(isolationOf(undefined)).toBe("local_cli");
  });
});

describe("metadataProblem", () => {
  const ok = { kind: "ok" } as const;

  it("mirrors the server's key shape", () => {
    expect(METADATA_KEY_RE.test("owner")).toBe(true);
    expect(METADATA_KEY_RE.test("cost.center-1_a")).toBe(true);
    expect(METADATA_KEY_RE.test("-lead")).toBe(false);
    expect(METADATA_KEY_RE.test("has space")).toBe(false);
    expect(METADATA_KEY_RE.test("k".repeat(64))).toBe(true);
    expect(METADATA_KEY_RE.test("k".repeat(65))).toBe(false);
  });

  it("passes a clean map and an empty one", () => {
    expect(metadataProblem([])).toEqual(ok);
    expect(
      metadataProblem([
        { id: 1, key: "owner", value: "platform" },
        { id: 2, key: "tier", value: "" },
      ]),
    ).toEqual(ok);
  });

  it("names the row and the field at fault, with the server's own limit", () => {
    expect(metadataProblem([{ id: 7, key: "has space", value: "x" }])).toEqual({
      kind: "row",
      id: 7,
      field: "key",
      message: expect.stringContaining("letter or digit"),
    });
    expect(metadataProblem([{ id: 7, key: "", value: "x" }])).toEqual({
      kind: "row",
      id: 7,
      field: "key",
      message: expect.stringContaining("required"),
    });
    expect(metadataProblem([{ id: 7, key: "owner", value: "v".repeat(1025) }])).toEqual({
      kind: "row",
      id: 7,
      field: "value",
      message: expect.stringContaining("1024"),
    });
    expect(
      metadataProblem([
        { id: 1, key: "owner", value: "a" },
        { id: 2, key: "owner", value: "b" },
      ]),
    ).toEqual({ kind: "row", id: 2, field: "key", message: expect.stringContaining("twice") });
  });

  it("caps the row count at the server's 32", () => {
    const rows = Array.from({ length: 33 }, (_, i) => ({ id: i, key: `k${i}`, value: "" }));
    expect(metadataProblem(rows)).toEqual({ kind: "map", message: expect.stringContaining("32") });
  });
});

describe("requiresStoredCredential", () => {
  it("is false only for the host-minted Bedrock type", () => {
    expect(requiresStoredCredential("codex_bedrock")).toBe(false);
    expect(requiresStoredCredential("codex_api")).toBe(true);
    expect(credentialState("codex_bedrock", [])).toBe("connected");
  });
});

describe("bedrock partition", () => {
  it("defaults a missing bag to GovCloud", () => {
    expect(bedrockRegionOf({})).toBe("us-gov-west-1");
    expect(bedrockModelOf({})).toBe("us-gov.xai.grok-4.6");
    expect(bedrockPartitionOf({})).toBe("gov");
  });

  it("reads a commercial row", () => {
    const details = { region: "us-east-1", model: "us.xai.grok-4.6" };
    expect(bedrockRegionOf(details)).toBe("us-east-1");
    expect(bedrockModelOf(details)).toBe("us.xai.grok-4.6");
    expect(bedrockPartitionOf(details)).toBe("commercial");
    expect(bedrockDefaults("commercial")).toEqual({
      region: "us-east-1",
      model: "us.xai.grok-4.6",
    });
  });
});
