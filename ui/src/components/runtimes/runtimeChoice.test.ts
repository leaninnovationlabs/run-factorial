import { describe, expect, it } from "vitest";
import {
  authCards,
  axesOf,
  type Choice,
  CLI_CARDS,
  cardOptions,
  cardStates,
  choiceForSandboxCard,
  choiceFromSlug,
  choiceLabels,
  detailsFor,
  fieldsFor,
  type SandboxCard,
  sandboxCardOf,
  sandboxCardOptions,
  suggestName,
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
    const states = cardStates(null, {
      cli: "claude",
      auth: "api_key",
      sandbox: "cloud",
      sandboxKey: "own",
    });
    expect(Object.values(states.cli).some((s) => s.enabled)).toBe(false);
  });

  it("shows no card the deployment leaves out or that is not built yet", () => {
    const states = cardStates(new Set(["claude_api@e2b"]), {
      cli: "claude",
      auth: "api_key",
      sandbox: "cloud",
      sandboxKey: "own",
    });
    const shown = (options: { value: string }[]) => options.map((o) => o.value);
    expect(shown(cardOptions(CLI_CARDS, states.cli))).toEqual(["claude"]);
    expect(shown(cardOptions(authCards("claude"), states.auth))).toEqual(["api_key"]);
    expect(shown(sandboxCardOptions(states.sandbox, new Set(), null, CHOICE))).toEqual(["own"]);
  });
});

const CHOICE: Choice = { cli: "claude", auth: "api_key", sandbox: "cloud", sandboxKey: "own" };

describe("the Sandbox step's one row", () => {
  const offered = new Set(["claude_api@e2b", "claude_subscription@e2b", "claude_api@local_cli"]);
  const covered = new Set(["claude_api@e2b"]);
  const optionsFor = (
    choice: Choice,
    managed: ReadonlySet<string> = covered,
    limit: number | null = 5,
  ) => sandboxCardOptions(cardStates(offered, choice).sandbox, managed, limit, choice);

  it("maps each card to the sandbox and key it stores, and back", () => {
    const cards: [SandboxCard, Partial<Choice>][] = [
      ["host", { sandbox: "host" }],
      ["managed", { sandbox: "cloud", sandboxKey: "managed" }],
      ["own", { sandbox: "cloud", sandboxKey: "own" }],
      ["self_hosted", { sandbox: "self_hosted" }],
    ];
    for (const [card, stored] of cards) {
      expect(choiceForSandboxCard(card)).toEqual(stored);
      expect(sandboxCardOf({ ...CHOICE, sandboxKey: "own", ...stored })).toBe(card);
    }
  });

  it("puts the managed card between the host and the own card, with the organization's limit", () => {
    const options = optionsFor({ ...CHOICE, sandboxKey: "managed" });
    expect(options.map((o) => [o.value, o.label, o.isDisabled])).toEqual([
      ["host", "On the host", false],
      ["managed", "Factorial-managed sandbox", false],
      ["own", "Your own E2B sandbox", false],
    ]);
    const managed = options[1];
    expect(managed?.description).toBe(
      "Runs in Factorial's sandbox account. No setup. Your organization gets 5 runs at a time.",
    );
    expect(managed?.lozenge?.text).toBe("Limited");
    expect(options[2]?.description).toBe(
      "Runs in your E2B account, with your limits and your template builds.",
    );
  });

  it("disables the managed card for an authentication it does not cover, and says why", () => {
    const managed = optionsFor({ ...CHOICE, auth: "subscription" }).find(
      (o) => o.value === "managed",
    );
    expect(managed?.isDisabled).toBe(true);
    expect(managed?.disabledReason).toBe("Works with an API key only");

    const otherCli = optionsFor(CHOICE, new Set(["codex_api@e2b"])).find(
      (o) => o.value === "managed",
    );
    expect(otherCli?.isDisabled).toBe(true);
    expect(otherCli?.disabledReason).toBe("Not available on this installation");
  });

  it("has no managed card when the organization may not use the managed key", () => {
    for (const [managed, limit] of [
      [covered, 0],
      [covered, null],
      [new Set<string>(), 5],
    ] as const) {
      expect(optionsFor(CHOICE, managed, limit).map((o) => o.value)).toEqual(["host", "own"]);
    }
  });

  it("names the chosen card in the summary", () => {
    expect(choiceLabels({ ...CHOICE, sandboxKey: "managed" }).sandbox).toBe(
      "Factorial-managed sandbox",
    );
    expect(choiceLabels(CHOICE).sandbox).toBe("Your own E2B sandbox");
  });

  it("suggests a name the whole organization can read, so never 'your own'", () => {
    expect(suggestName({ ...CHOICE, sandboxKey: "managed" })).toMatch(
      / · Factorial-managed sandbox$/,
    );
    expect(suggestName(CHOICE)).toMatch(/ · E2B sandbox$/);
    expect(suggestName({ ...CHOICE, sandbox: "host" })).not.toContain("sandbox");
  });
});

describe("Bedrock", () => {
  const offered = new Set(["codex_bedrock@local_cli", "claude_api@e2b"]);

  it("is codex_bedrock for Codex on either partition, and still coming for Claude", () => {
    expect(typeFor("codex", "bedrock_govcloud")).toBe("codex_bedrock");
    expect(typeFor("codex", "bedrock_commercial")).toBe("codex_bedrock");
    expect(typeFor("claude", "bedrock_govcloud")).toBeNull();
    const claude = cardStates(offered, {
      cli: "claude",
      auth: "api_key",
      sandbox: "cloud",
      sandboxKey: "own",
    });
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
      sandboxKey: "own",
    });
    expect(codex.auth.bedrock_commercial.enabled).toBe(true);
    expect(codex.sandbox.host.enabled).toBe(true);
    expect(codex.sandbox.cloud.enabled).toBe(false);
  });

  it("asks for no key, since the host role mints the bearer", () => {
    expect(
      fieldsFor(
        { cli: "codex", auth: "bedrock_govcloud", sandbox: "host", sandboxKey: "own" },
        "chatgpt",
      ),
    ).toEqual([]);
  });

  it("sends the partition's region and model in the details", () => {
    expect(
      detailsFor({ cli: "codex", auth: "bedrock_govcloud", sandbox: "host", sandboxKey: "own" }),
    ).toEqual({
      isolation: "local_cli",
      region: "us-gov-west-1",
      model: "us-gov.xai.grok-4.6",
    });
    expect(
      detailsFor({ cli: "codex", auth: "bedrock_commercial", sandbox: "host", sandboxKey: "own" }),
    ).toEqual({
      isolation: "local_cli",
      region: "us-east-1",
      model: "us.xai.grok-4.6",
    });
    expect(
      detailsFor({ cli: "claude", auth: "api_key", sandbox: "cloud", sandboxKey: "own" }),
    ).toEqual({
      isolation: "e2b",
    });
  });

  it("proposes the slugs processes use and reads them back", () => {
    const slug = (
      cli: "claude" | "codex",
      auth: "api_key" | "subscription",
      sandbox: "host" | "cloud",
    ) => suggestSlugFor({ cli, auth, sandbox, sandboxKey: "own" });
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
