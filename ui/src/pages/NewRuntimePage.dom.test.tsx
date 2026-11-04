// @vitest-environment happy-dom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../api/client";
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
    qc.setQueryData(keys.runtimeOptions, { offered: ["claude_api@e2b"] });
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
    await click(byText(root, "button", "Test credentials"));
    await settle();
    expect(root.querySelector("select")?.value).toBe(BUILD.pin);

    await type(e2bKey, "e2b_two");
    await click(byText(root, "button", "Create runtime"));
    await settle();

    expect(create).toHaveBeenCalledTimes(1);
    expect(create.mock.calls[0]?.[0].details).toEqual({ isolation: "e2b" });
  });

  it("creates a Bedrock runtime with its partition's endpoint and no key", async () => {
    const create = vi
      .spyOn(api, "createRuntime")
      .mockResolvedValue({ slug: "codex-aws-bedrock-govcloud" } as Runtime);
    const qc = new QueryClient();
    qc.setQueryData(keys.runtimeOptions, { offered: ["codex_bedrock@local_cli"] });
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
    qc.setQueryData(keys.runtimeOptions, { offered: ["claude_api@e2b"] });
    view = await mountPage(qc);
    const cards = view.container.querySelectorAll<HTMLInputElement>(
      'input[name="cli"], input[name="auth"], input[name="sandbox"]',
    );
    const values = [...cards].map((radio) => radio.value);
    expect(values).toEqual(["claude", "api_key", "cloud"]);
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
