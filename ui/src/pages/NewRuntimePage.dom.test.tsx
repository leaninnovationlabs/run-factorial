// @vitest-environment happy-dom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../api/client";
import { organizationDetail } from "../api/organization.fixture";
import { keys } from "../api/queries";
import type { Runtime, RuntimeTemplateBuild } from "../api/types";
import { byText, click, type Mounted, mount, settle, type } from "../testDom";
import { NewRuntimePage } from "./NewRuntimePage";

let view: Mounted | null = null;
afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.restoreAllMocks();
});

const BUILD: RuntimeTemplateBuild = {
  pin: "factorial-claude-2:abcd1234",
  template: "factorial-claude-2",
  build_id: "abcd1234-0000",
  created_at: "2026-10-20T09:00:00Z",
  cpu_count: 2,
  memory_mb: 4096,
  matches_harness: true,
  verified: true,
};

describe("NewRuntimePage", () => {
  it("drops the template when a key changes after the test", async () => {
    // The template was listed with the key that passed; another key may name another team,
    // whose sandboxes cannot boot that build.
    vi.spyOn(api, "checkRuntime").mockResolvedValue({
      model: { ok: true, detail: "ok" },
      local: false,
      e2b: { ok: true, detail: "ok" },
      templates: [BUILD],
    });
    const create = vi
      .spyOn(api, "createRuntime")
      .mockResolvedValue({ slug: "claude-code-api-key" } as Runtime);
    const qc = new QueryClient();
    seed(qc, ["claude_api@e2b"]);
    view = await mount(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/runtimes/new"]}>
          <Routes>
            <Route path="/runtimes/new" element={<NewRuntimePage />} />
            <Route path="/runtimes/:slug" element={<p>runtime page</p>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const root = view.container;
    const e2bKey = root.querySelector('input[name="E2B_API_KEY"]');
    await type(root.querySelector('input[name="ANTHROPIC_API_KEY"]'), "sk-ant-one");
    await type(e2bKey, "e2b_one");
    await click(testButton(e2bKey));
    await settle();
    expect(root.querySelector("select")?.value).toBe(BUILD.pin);

    await type(e2bKey, "e2b_two");
    await click(byText(root, "button", "Create runtime"));
    await settle();

    expect(create).toHaveBeenCalledTimes(1);
    expect(create.mock.calls[0]?.[0].details).toEqual({ isolation: "e2b" });
  });

  it("has four numbered steps and none titled Credentials", async () => {
    view = await mountOffered(["claude_api@e2b"]);
    const titles = [...view.container.querySelectorAll("h2")].map((each) => each.textContent);
    expect(titles).toEqual(expect.arrayContaining(["1. CLI", "2. Authentication", "3. Sandbox"]));
    expect(titles).toContain("4. Name");
    expect(titles.some((title) => title?.includes("Credentials"))).toBe(false);
    expect(titles.some((title) => title?.startsWith("5."))).toBe(false);
  });

  it("puts each key and its Test key button under its card, with no helper sentence", async () => {
    view = await mountOffered(["claude_api@e2b"]);
    const root = view.container;
    expect(testButton(field(root, "ANTHROPIC_API_KEY")).type).toBe("button");
    expect(testButton(field(root, "E2B_API_KEY")).type).toBe("button");
    expect(root.textContent).not.toContain("Create one at");
    expect(root.textContent).not.toContain("Makes one small call");
    expect(root.querySelector("select")?.disabled).toBe(true);
    expect(root.querySelector("select")?.textContent).toBe("Test the key to list the templates");
  });

  it("tests one key at a time and keeps a passed sandbox test through a model key edit", async () => {
    const check = vi.spyOn(api, "checkRuntime").mockResolvedValue({
      model: { ok: true, detail: "model ok" },
      local: false,
      e2b: { ok: true, detail: "sandbox ok" },
      templates: [BUILD],
    });
    view = await mountOffered(["claude_api@e2b"]);
    const root = view.container;
    await type(field(root, "ANTHROPIC_API_KEY"), "sk-ant-one");
    await type(field(root, "E2B_API_KEY"), "e2b_one");

    await click(testButton(field(root, "ANTHROPIC_API_KEY")));
    await settle();
    expect(check.mock.calls[0]?.[0].credentials).toEqual({ ANTHROPIC_API_KEY: "sk-ant-one" });
    expect(root.textContent).toContain("model ok");
    expect(root.textContent).not.toContain("sandbox ok");
    expect(root.querySelector("select")?.disabled).toBe(true);

    await click(testButton(field(root, "E2B_API_KEY")));
    await settle();
    expect(check.mock.calls[1]?.[0].credentials).toEqual({ E2B_API_KEY: "e2b_one" });
    expect(root.textContent).toContain("sandbox ok");
    expect(root.querySelector("select")?.disabled).toBe(false);
    expect(root.querySelector("select")?.value).toBe(BUILD.pin);

    await type(field(root, "ANTHROPIC_API_KEY"), "sk-ant-two");
    expect(root.textContent).not.toContain("model ok");
    expect(root.textContent).toContain("sandbox ok");
    expect(root.querySelector("select")?.value).toBe(BUILD.pin);
  });

  it("keeps a test pending while the other key's test finishes first", async () => {
    const answers: ((out: Awaited<ReturnType<typeof api.checkRuntime>>) => void)[] = [];
    vi.spyOn(api, "checkRuntime").mockImplementation(
      () => new Promise((resolve) => answers.push(resolve)),
    );
    view = await mountOffered(["claude_api@e2b"]);
    const root = view.container;
    await type(field(root, "ANTHROPIC_API_KEY"), "sk-ant-one");
    await type(field(root, "E2B_API_KEY"), "e2b_one");
    const busy = (name: string) => testButton(field(root, name)).getAttribute("aria-busy");
    await click(testButton(field(root, "ANTHROPIC_API_KEY")));
    await click(testButton(field(root, "E2B_API_KEY")));
    expect([busy("ANTHROPIC_API_KEY"), busy("E2B_API_KEY")]).toEqual(["true", "true"]);

    answers[1]?.({
      model: { ok: true, detail: "ok" },
      local: false,
      e2b: { ok: true, detail: "ok" },
      templates: [],
    });
    await settle();

    expect(busy("ANTHROPIC_API_KEY")).toBe("true");
    expect(busy("E2B_API_KEY")).not.toBe("true");
  });

  it("names each Test key button for its field, and each sends only its own credential", async () => {
    const check = vi.spyOn(api, "checkRuntime").mockResolvedValue({
      model: { ok: true, detail: "ok" },
      local: false,
      e2b: { ok: true, detail: "ok" },
      templates: [],
    });
    view = await mountOffered(["claude_api@e2b"]);
    const root = view.container;
    const names = [...root.querySelectorAll("button")]
      .map((button) => button.textContent)
      .filter((text) => text?.startsWith("Test key"));
    expect(names).toEqual(["Test key for Anthropic API key", "Test key for Sandbox API key"]);
    await type(field(root, "ANTHROPIC_API_KEY"), "sk-ant-one");
    await type(field(root, "E2B_API_KEY"), "e2b_one");
    await click(byText(root, "button", "Test key for Sandbox API key"));
    await click(byText(root, "button", "Test key for Anthropic API key"));
    expect(check.mock.calls.map((call) => call[0].credentials)).toEqual([
      { E2B_API_KEY: "e2b_one" },
      { ANTHROPIC_API_KEY: "sk-ant-one" },
    ]);
  });

  it("says so when the tested team has no ready builds", async () => {
    vi.spyOn(api, "checkRuntime").mockResolvedValue({
      model: { ok: true, detail: "ok" },
      local: false,
      e2b: { ok: true, detail: "ok" },
      templates: [],
    });
    view = await mountOffered(["claude_api@e2b"]);
    await click(testButton(field(view.container, "E2B_API_KEY")));
    await settle();
    const select = view.container.querySelector("select");
    expect(select?.disabled).toBe(true);
    expect(select?.textContent).toBe(
      "This team has no ready builds. Build one, then choose it on the runtime's page.",
    );
  });

  it("clears the model outcome when the authentication card changes", async () => {
    vi.spyOn(api, "checkRuntime").mockResolvedValue({
      model: { ok: true, detail: "model ok" },
      local: false,
      e2b: null,
      templates: [],
    });
    view = await mountOffered(["claude_api@local_cli", "claude_subscription@local_cli"]);
    const root = view.container;
    await click(testButton(field(root, "ANTHROPIC_API_KEY")));
    await settle();
    expect(root.textContent).toContain("model ok");
    await click(root.querySelector('input[value="subscription"]'));
    expect(root.textContent).not.toContain("model ok");
    expect(field(root, "CLAUDE_CODE_OAUTH_TOKEN")).not.toBeNull();
  });

  it("drops the sandbox outcome when the sandbox card changes", async () => {
    vi.spyOn(api, "checkRuntime").mockResolvedValue({
      model: { ok: true, detail: "ok" },
      local: false,
      e2b: { ok: true, detail: "sandbox ok" },
      templates: [BUILD],
    });
    view = await mountOffered(["claude_api@e2b", "claude_api@local_cli"]);
    const root = view.container;
    await click(root.querySelector('input[value="own"]'));
    await click(testButton(field(root, "E2B_API_KEY")));
    await settle();
    expect(root.textContent).toContain("sandbox ok");
    await click(root.querySelector('input[value="host"]'));
    await click(root.querySelector('input[value="own"]'));
    expect(root.textContent).not.toContain("sandbox ok");
    expect(root.querySelector("select")?.disabled).toBe(true);
  });

  it("uses the same row for a pasted auth.json and offers no test for a ChatGPT sign-in", async () => {
    view = await mountOffered(["codex_subscription@local_cli"]);
    const root = view.container;
    expect(root.textContent).not.toContain("Test key");
    expect(root.textContent).not.toContain("Once the runtime exists");
    await click(root.querySelector('input[value="paste"]'));
    const area = root.querySelector('textarea[name="CODEX_AUTH_JSON"]');
    expect(testButton(area).textContent).toContain("Test key");
  });

  it("says a Bedrock runtime has no key to store", async () => {
    view = await mountOffered(["codex_bedrock@local_cli"]);
    const root = view.container;
    expect(root.textContent).toContain(
      "No key to store. Each run gets a short-lived token from the host's AWS role.",
    );
    expect(root.textContent).not.toContain("Test key");
  });

  it("focuses the first empty required field on Create", async () => {
    view = await mountOffered(["claude_api@e2b"]);
    const root = view.container;
    await click(byText(root, "button", "Create runtime"));
    await settle();
    await settle();
    expect(document.activeElement).toBe(field(root, "ANTHROPIC_API_KEY"));
  });

  describe("the Factorial-managed sandbox", () => {
    const COVERED = ["claude_api@e2b"];
    const sandboxCard = (root: ParentNode, value: string) =>
      root.querySelector<HTMLInputElement>(`input[name="sandbox"][value="${value}"]`);
    const chosenCard = (root: ParentNode) =>
      root.querySelector<HTMLInputElement>('input[name="sandbox"]:checked')?.value;

    it("is the default in the Sandbox step's one row, and opens nothing", async () => {
      view = await mountOffered(["claude_api@e2b"], COVERED, 5);
      const root = view.container;
      const step = sandboxCard(root, "managed")?.closest("fieldset")?.parentElement;
      expect(step?.querySelectorAll("fieldset")).toHaveLength(1);
      const cards = [...root.querySelectorAll<HTMLInputElement>('input[name="sandbox"]')];
      expect(cards.map((card) => [card.value, card.checked])).toEqual([
        ["managed", true],
        ["own", false],
      ]);
      expect(root.textContent).toContain(
        "Runs in Factorial's sandbox account. No setup. Your organization gets 5 runs at a time.",
      );
      expect(field(root, "E2B_API_KEY")).toBeNull();
      expect(root.querySelector("select")).toBeNull();
      expect(root.textContent).toContain("Factorial-managed sandbox");
      expect(root.textContent).toContain("Up to 5 runs at a time for this organization.");
      expect(root.textContent).not.toContain("Needs a template before it can run.");
      expect(root.textContent).not.toMatch(/Stores[\s\S]*E2B_API_KEY/);
    });

    it("says one run in the singular", async () => {
      view = await mountOffered(COVERED, COVERED, 1);
      expect(view.container.textContent).toContain("Your organization gets 1 run at a time.");
      expect(view.container.textContent).toContain("Up to 1 run at a time for this organization.");
    });

    it("is not offered when the limit is 0 or null, even if the offer is covered", async () => {
      for (const limit of [0, null]) {
        view = await mountOffered(COVERED, COVERED, limit);
        const root = view.container;
        expect(sandboxCard(root, "managed")).toBeNull();
        expect(chosenCard(root)).toBe("own");
        expect(field(root, "E2B_API_KEY")).not.toBeNull();
        expect(root.textContent).toContain("Your own E2B sandbox");
        await view.unmount();
        view = null;
      }
    });

    it("is not offered when the deployment's managed key covers nothing", async () => {
      view = await mountOffered(COVERED, [], 5);
      expect(sandboxCard(view.container, "managed")).toBeNull();
      expect(field(view.container, "E2B_API_KEY")).not.toBeNull();
    });

    it("is disabled for a type the managed key does not cover", async () => {
      view = await mountOffered(["claude_api@e2b"], ["codex_api@e2b"], 3);
      const root = view.container;
      expect(sandboxCard(root, "managed")?.disabled).toBe(true);
      expect(root.textContent).toContain("Not available on this installation");
      expect(chosenCard(root)).toBe("own");
      expect(field(root, "E2B_API_KEY")).not.toBeNull();
    });

    it("is disabled with its reason for a non-API authentication, and the own card is chosen", async () => {
      view = await mountOffered(["claude_api@e2b", "claude_subscription@e2b"], COVERED, 5);
      const root = view.container;
      await click(root.querySelector('input[name="auth"][value="subscription"]'));
      expect(sandboxCard(root, "managed")?.disabled).toBe(true);
      expect(root.textContent).toContain("Works with an API key only");
      expect(chosenCard(root)).toBe("own");
      expect(field(root, "E2B_API_KEY")).not.toBeNull();
    });

    it("shows the key row and the Template box under the own card only", async () => {
      view = await mountOffered(COVERED, COVERED, 5);
      const root = view.container;
      await click(sandboxCard(root, "own"));
      expect(field(root, "E2B_API_KEY")).not.toBeNull();
      expect(root.querySelector("select")).not.toBeNull();
      await click(sandboxCard(root, "managed"));
      expect(field(root, "E2B_API_KEY")).toBeNull();
      expect(root.querySelector("select")).toBeNull();
    });

    it("ends a covered, uncovered, covered round trip on managed, and on own for a person who picked own", async () => {
      const offered = ["claude_api@e2b", "claude_subscription@e2b"];
      view = await mountOffered(offered, COVERED, 5);
      const root = view.container;
      await click(root.querySelector('input[name="auth"][value="subscription"]'));
      expect(chosenCard(root)).toBe("own");
      await click(root.querySelector('input[name="auth"][value="api_key"]'));
      expect(chosenCard(root)).toBe("managed");

      await click(sandboxCard(root, "own"));
      await click(root.querySelector('input[name="auth"][value="subscription"]'));
      await click(root.querySelector('input[name="auth"][value="api_key"]'));
      expect(chosenCard(root)).toBe("own");
    });

    it("does not keep a click on the own card as a pick while the managed card is disabled", async () => {
      // With the host offered, the own card is the only way to the cloud under Subscription.
      const offered = ["claude_api@local_cli", "claude_subscription@local_cli", ...COVERED];
      view = await mountOffered([...offered, "claude_subscription@e2b"], COVERED, 5);
      const root = view.container;
      await click(root.querySelector('input[name="auth"][value="subscription"]'));
      await click(sandboxCard(root, "own"));
      expect(chosenCard(root)).toBe("own");
      await click(root.querySelector('input[name="auth"][value="api_key"]'));
      expect(chosenCard(root)).toBe("managed");
      expect(field(root, "E2B_API_KEY")).toBeNull();
    });

    it("shows no sandbox cards until the organization is read, then the managed one chosen", async () => {
      vi.spyOn(api, "getOrganization").mockReturnValue(new Promise(() => {}));
      const qc = new QueryClient();
      qc.setQueryData(keys.runtimeOptions, { offered: COVERED, managed_sandbox: COVERED });
      view = await mountPage(qc);
      const root = view.container;
      expect(root.querySelector('input[name="sandbox"]')).toBeNull();
      qc.setQueryData(keys.organization, { ...organizationDetail(), managed_sandbox_limit: 5 });
      await settle();
      expect(chosenCard(root)).toBe("managed");
    });

    it("shows the cards without the managed one when the organization can't be read", async () => {
      vi.spyOn(api, "getOrganization").mockRejectedValue(new ApiError(500, "Boom"));
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      qc.setQueryData(keys.runtimeOptions, { offered: COVERED, managed_sandbox: COVERED });
      view = await mountPage(qc);
      await settle();
      const root = view.container;
      expect(sandboxCard(root, "own")).not.toBeNull();
      expect(sandboxCard(root, "managed")).toBeNull();
      expect(chosenCard(root)).toBe("own");
    });

    it("sends sandbox_key managed with no E2B key and no template, though one was typed", async () => {
      const create = vi
        .spyOn(api, "createRuntime")
        .mockResolvedValue({ slug: "claude-api-on-e2b" } as Runtime);
      view = await mountOffered(COVERED, COVERED, 5);
      const root = view.container;
      await click(sandboxCard(root, "own"));
      await type(field(root, "E2B_API_KEY"), "e2b_typed");
      expect(root.textContent).toContain("Your own E2B sandbox");
      await click(sandboxCard(root, "managed"));
      expect(field(root, "E2B_API_KEY")).toBeNull();
      await type(field(root, "ANTHROPIC_API_KEY"), "sk-ant-one");
      await click(byText(root, "button", "Create runtime"));
      await settle();
      const body = create.mock.calls[0]?.[0];
      expect(body?.details).toEqual({ isolation: "e2b", sandbox_key: "managed" });
      expect(body?.credentials).toEqual({ ANTHROPIC_API_KEY: "sk-ant-one" });
    });

    /** A subscription is not covered, so the key is forced own, and its test lists a build. */
    async function testedUnderForcedOwn(): Promise<HTMLElement> {
      vi.spyOn(api, "checkRuntime").mockResolvedValue({
        model: { ok: true, detail: "ok" },
        local: false,
        e2b: { ok: true, detail: "ok" },
        templates: [BUILD],
      });
      view = await mountOffered(["claude_api@e2b", "claude_subscription@e2b"], COVERED, 5);
      const root = view.container;
      await click(root.querySelector('input[name="auth"][value="subscription"]'));
      await type(field(root, "E2B_API_KEY"), "e2b_one");
      await click(testButton(field(root, "E2B_API_KEY")));
      await settle();
      expect(root.querySelector("select")?.value).toBe(BUILD.pin);
      return root;
    }

    it("sends no template once another card makes the key managed", async () => {
      const create = vi
        .spyOn(api, "createRuntime")
        .mockResolvedValue({ slug: "claude-api-on-e2b" } as Runtime);
      const root = await testedUnderForcedOwn();

      await click(root.querySelector('input[name="auth"][value="api_key"]'));
      expect(field(root, "E2B_API_KEY")).toBeNull();
      await type(field(root, "ANTHROPIC_API_KEY"), "sk-ant-one");
      await click(byText(root, "button", "Create runtime"));
      await settle();

      expect(create.mock.calls[0]?.[0].details).toEqual({
        isolation: "e2b",
        sandbox_key: "managed",
      });
    });

    it("drops the sandbox test when another card moves the key to managed and back", async () => {
      const root = await testedUnderForcedOwn();

      await click(root.querySelector('input[name="auth"][value="api_key"]'));
      await click(root.querySelector('input[name="auth"][value="subscription"]'));

      expect(root.querySelector("select")?.disabled).toBe(true);
    });

    it("keeps the own card's key field and template box, and the slug does not change", async () => {
      view = await mountOffered(COVERED, COVERED, 5);
      const root = view.container;
      const slug = () => (root.querySelector("#field-slug") as HTMLInputElement).value;
      const slugBefore = slug();
      await click(sandboxCard(root, "own"));
      expect(field(root, "E2B_API_KEY")).not.toBeNull();
      expect(root.querySelector("select")?.disabled).toBe(true);
      expect(root.textContent).toContain("Your own E2B sandbox");
      expect(root.textContent).toContain("Needs a template before it can run.");
      expect(slug()).toBe(slugBefore);
    });

    it("tests the model key with sandbox_key managed", async () => {
      const check = vi.spyOn(api, "checkRuntime").mockResolvedValue({
        model: { ok: true, detail: "ok" },
        local: false,
        e2b: null,
        templates: [],
      });
      view = await mountOffered(COVERED, COVERED, 5);
      const root = view.container;
      await type(field(root, "ANTHROPIC_API_KEY"), "sk-ant-one");
      await click(byText(root, "button", "Test key for Anthropic API key"));
      await settle();
      expect(check.mock.calls[0]?.[0].details).toEqual({
        isolation: "e2b",
        sandbox_key: "managed",
      });
      expect(check.mock.calls[0]?.[0].credentials).toEqual({ ANTHROPIC_API_KEY: "sk-ant-one" });
    });

    it("shows the API's sentence when it refuses a managed create", async () => {
      vi.spyOn(api, "createRuntime").mockRejectedValue(
        new ApiError(
          422,
          "The Factorial-managed key is not available to organization 'default'. Use your own E2B key instead.",
        ),
      );
      view = await mountOffered(COVERED, COVERED, 5);
      const root = view.container;
      await type(field(root, "ANTHROPIC_API_KEY"), "sk-ant-one");
      await click(byText(root, "button", "Create runtime"));
      await settle();
      expect(root.textContent).toContain("Use your own E2B key instead.");
    });
  });

  it("creates a Bedrock runtime with its partition's endpoint and no key", async () => {
    const create = vi
      .spyOn(api, "createRuntime")
      .mockResolvedValue({ slug: "codex-aws-bedrock-govcloud" } as Runtime);
    const qc = new QueryClient();
    seed(qc, ["codex_bedrock@local_cli"]);
    view = await mount(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/runtimes/new"]}>
          <Routes>
            <Route path="/runtimes/new" element={<NewRuntimePage />} />
            <Route path="/runtimes/:slug" element={<p>runtime page</p>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await click(view.container.querySelector('input[value="bedrock_govcloud"]'));
    await click(byText(view.container, "button", "Create runtime"));
    await settle();

    expect(create).toHaveBeenCalledTimes(1);
    const body = create.mock.calls[0]?.[0];
    expect(body?.type).toBe("codex_bedrock");
    expect(body?.details).toEqual({
      isolation: "local_cli",
      region: "us-gov-west-1",
      model: "us-gov.xai.grok-4.6",
    });
    expect(body).not.toHaveProperty("credentials");
  });

  it("shows no card for a runtime the deployment leaves out or that is not built yet", async () => {
    const qc = new QueryClient();
    seed(qc, ["claude_api@e2b"]);
    view = await mountPage(qc);
    const cards = view.container.querySelectorAll<HTMLInputElement>(
      'input[name="cli"], input[name="auth"], input[name="sandbox"]',
    );
    const values = [...cards].map((radio) => radio.value);
    expect(values).toEqual(["claude", "api_key", "own"]);
  });

  it("shows no cards when it cannot read what the deployment offers, only the error", async () => {
    vi.spyOn(api, "runtimeOptions").mockRejectedValue(new ApiError(403, "Forbidden"));
    view = await mountPage(new QueryClient());
    await settle();
    const root = view.container;
    expect(root.querySelector('input[type="radio"]')).toBeNull();
    expect(root.textContent).toContain("Couldn't load the available options");
    expect(byText(root, "button", "Try again")).not.toBeNull();
  });
});

function mountPage(qc: QueryClient): Promise<Mounted> {
  return mount(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/runtimes/new"]}>
        <Routes>
          <Route path="/runtimes/new" element={<NewRuntimePage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The options the deployment offers, the managed offers it covers, and the organization's limit
 *  on the managed key (null: the deployment provides none). */
function seed(
  qc: QueryClient,
  offered: string[],
  managed: string[] = [],
  limit: number | null = null,
): void {
  qc.setQueryData(keys.runtimeOptions, { offered, managed_sandbox: managed });
  qc.setQueryData(keys.organization, { ...organizationDetail(), managed_sandbox_limit: limit });
}

function mountOffered(
  offered: string[],
  managed: string[] = [],
  limit: number | null = null,
): Promise<Mounted> {
  const qc = new QueryClient();
  seed(qc, offered, managed, limit);
  return mountPage(qc);
}

function field(root: ParentNode, name: string): HTMLInputElement {
  return root.querySelector(`[name="${name}"]`) as HTMLInputElement;
}

/** The Test key button in the row of a field. */
function testButton(control: Element | null): HTMLButtonElement {
  return control?.parentElement?.querySelector("button") as HTMLButtonElement;
}
