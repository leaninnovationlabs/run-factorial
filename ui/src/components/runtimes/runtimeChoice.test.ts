import { describe, expect, it } from "vitest";
import {
  authCards,
  axesOf,
  CLI_CARDS,
  cardOptions,
  cardStates,
  choiceFromSlug,
  detailsFor,
  fieldsFor,
  SANDBOX_CARDS,
  suggestSlugFor,
  typeFor,
} from "./runtimeChoice";
import { RUNTIME_TYPES } from "./runtimes";

describe("runtimeChoice", () => {
  it("maps every stored type to its cards and back", () => {
    for (const type of RUNTIME_TYPES) {
      const { cli, auth } = axesOf(type);
      expect(typeFor(cli, auth)).toBe(type);
    }
  });

  it("offers nothing when the list cannot be read", () => {
    const states = cardStates(null, { cli: "claude", auth: "api_key", sandbox: "cloud" });
    expect(Object.values(states.cli).some((s) => s.enabled)).toBe(false);
  });

  it("shows no card the deployment leaves out or that is not built yet", () => {
    const states = cardStates(new Set(["claude_api@e2b"]), {
      cli: "claude",
      auth: "api_key",
      sandbox: "cloud",
    });
    const shown = (options: { value: string }[]) => options.map((o) => o.value);
    expect(shown(cardOptions(CLI_CARDS, states.cli))).toEqual(["claude"]);
    expect(shown(cardOptions(authCards("claude"), states.auth))).toEqual(["api_key"]);
    expect(shown(cardOptions(SANDBOX_CARDS, states.sandbox))).toEqual(["cloud"]);
  });
});

describe("Bedrock", () => {
  const offered = new Set(["codex_bedrock@local_cli", "claude_api@e2b"]);

  it("is codex_bedrock for Codex on either partition, and still coming for Claude", () => {
    expect(typeFor("codex", "bedrock_govcloud")).toBe("codex_bedrock");
    expect(typeFor("codex", "bedrock_commercial")).toBe("codex_bedrock");
    expect(typeFor("claude", "bedrock_govcloud")).toBeNull();
    const claude = cardStates(offered, { cli: "claude", auth: "api_key", sandbox: "cloud" });
    expect(claude.auth.bedrock_govcloud.enabled).toBe(false);
  });

  it("reads a stored row's partition from its region", () => {
    expect(axesOf("codex_bedrock", {})).toEqual({ cli: "codex", auth: "bedrock_govcloud" });
    expect(axesOf("codex_bedrock", { region: "us-east-1" })).toEqual({
      cli: "codex",
      auth: "bedrock_commercial",
    });
  });

  it("is offered on the host only", () => {
    const codex = cardStates(offered, {
      cli: "codex",
      auth: "bedrock_commercial",
      sandbox: "host",
    });
    expect(codex.auth.bedrock_commercial.enabled).toBe(true);
    expect(codex.sandbox.host.enabled).toBe(true);
    expect(codex.sandbox.cloud.enabled).toBe(false);
  });

  it("asks for no key, since the host role mints the bearer", () => {
    expect(
      fieldsFor({ cli: "codex", auth: "bedrock_govcloud", sandbox: "host" }, "chatgpt"),
    ).toEqual([]);
  });

  it("sends the partition's region and model in the details", () => {
    expect(detailsFor({ cli: "codex", auth: "bedrock_govcloud", sandbox: "host" })).toEqual({
      isolation: "local_cli",
      region: "us-gov-west-1",
      model: "us-gov.xai.grok-4.6",
    });
    expect(detailsFor({ cli: "codex", auth: "bedrock_commercial", sandbox: "host" })).toEqual({
      isolation: "local_cli",
      region: "us-east-1",
      model: "us.xai.grok-4.6",
    });
    expect(detailsFor({ cli: "claude", auth: "api_key", sandbox: "cloud" })).toEqual({
      isolation: "e2b",
    });
  });

  it("proposes the slugs processes use and reads them back", () => {
    const slug = (
      cli: "claude" | "codex",
      auth: "api_key" | "subscription",
      sandbox: "host" | "cloud",
    ) => suggestSlugFor({ cli, auth, sandbox });
    expect(slug("claude", "api_key", "host")).toBe("claude-api");
    expect(slug("claude", "api_key", "cloud")).toBe("claude-api-on-e2b");
    expect(slug("codex", "subscription", "host")).toBe("codex-sub");
    expect(slug("codex", "subscription", "cloud")).toBe("codex-sub-on-e2b");
    expect(choiceFromSlug("claude-api-on-e2b")).toEqual({
      cli: "claude",
      auth: "api_key",
      sandbox: "cloud",
    });
    expect(choiceFromSlug("my-runtime")).toBeNull();
  });
});
