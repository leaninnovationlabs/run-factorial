// @vitest-environment happy-dom
/**
 * The runtime page in a DOM: the chosen build's verdict, and Test credential.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { organizationDetail } from "../../api/organization.fixture";
import { keys } from "../../api/queries";
import type {
  CredentialTest,
  Runtime,
  RuntimeProject,
  RuntimeTemplateBuild,
} from "../../api/types";
import { click, type Mounted, mount, settle } from "../../testDom";
import { RuntimeDetail } from "./RuntimeDetail";

const AT = "2026-09-01T09:00:00Z";

function e2bRuntime(overrides: Partial<Runtime> = {}): Runtime {
  return {
    id: 31,
    slug: "claude-e2b",
    name: "claude e2b",
    type: "claude_api",
    details: { isolation: "e2b", template: "factorial-claude-2-1-284:a1b2c3d4" },
    metadata: {},
    credential_names: ["ANTHROPIC_API_KEY", "E2B_API_KEY"],
    credentials: [
      { name: "ANTHROPIC_API_KEY", created_at: AT, updated_at: AT },
      { name: "E2B_API_KEY", created_at: AT, updated_at: AT },
    ],
    connected_projects: 0,
    last_auth_failure: null,
    sandbox_key: "own",
    template: null,
    sandbox_gap: null,
    created_at: AT,
    updated_at: AT,
    ...overrides,
  };
}

let view: Mounted | null = null;
let patches: unknown[] = [];
let tested: CredentialTest | null = null;
let patchRefusal: { status: number; detail: string } | null = null;

beforeEach(() => {
  patches = [];
  tested = null;
  patchRefusal = null;
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      if (init?.method === "POST" && url.endsWith("/test-credential") && tested) {
        return Promise.resolve(new Response(JSON.stringify(tested), { status: 200 }));
      }
      if (init?.method !== "PATCH") return Promise.reject(new TypeError("offline"));
      const body = JSON.parse(String(init.body)) as { details: Record<string, unknown> };
      patches.push(body);
      if (patchRefusal) {
        const refusal = { detail: patchRefusal.detail };
        return Promise.resolve(
          new Response(JSON.stringify(refusal), { status: patchRefusal.status }),
        );
      }
      const saved = e2bRuntime({ details: body.details });
      return Promise.resolve(new Response(JSON.stringify(saved), { status: 200 }));
    }),
  );
});

afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.unstubAllGlobals();
});

async function page(
  runtime: Runtime,
  builds?: RuntimeTemplateBuild[],
  projects: RuntimeProject[] = [],
  deployment: { managed: string[]; limit: number | null } = { managed: [], limit: null },
): Promise<HTMLElement> {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Number.POSITIVE_INFINITY } },
  });
  qc.setQueryData(keys.runtimeProjects(runtime.id), projects);
  qc.setQueryData(keys.runtimeOptions, { offered: [], managed_sandbox: deployment.managed });
  qc.setQueryData(keys.organization, {
    ...organizationDetail(),
    managed_sandbox_limit: deployment.limit,
  });
  if (builds) qc.setQueryData(keys.runtimeTemplates(runtime.id), builds);
  view = await mount(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <RuntimeDetail runtime={runtime} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return view.container;
}

describe("the runtime's connected repositories", () => {
  it("opens each repository on its project's own host", async () => {
    const root = await page(e2bRuntime({ connected_projects: 2 }), undefined, [
      { id: 1, name: "Widgets", github_repo: "acme/widgets" },
      {
        id: 2,
        name: "Ledger",
        github_repo: "finance/ledger",
        forge: {
          kind: "gitlab",
          name: "GitLab",
          origin: "https://gitlab.example.com",
          pull_noun: "MR",
          issue_path: "/-/issues/{key}",
          pull_path: "/-/merge_requests/{number}",
          tree_path: "/-/tree/{branch}",
          commit_path: "/-/commit/{sha}",
        },
      },
    ]);
    const href = (repo: string) =>
      [...root.querySelectorAll("a")]
        .find((a) => a.textContent?.trim() === repo)
        ?.getAttribute("href");
    expect(href("acme/widgets")).toBe("https://github.com/acme/widgets");
    expect(href("finance/ledger")).toBe("https://gitlab.example.com/finance/ledger");
  });
});

describe("an E2B runtime's chosen build", () => {
  it("says the worker refuses an unverified or unlisted build, with the vendor's smoke", async () => {
    const unverified = await page(e2bRuntime(), [build({ verified: false })]);
    expect(unverified.textContent).toContain(UNVERIFIED_NOTE);
    expect(unverified.textContent).toContain("ANTHROPIC_API_KEY=YOUR_MODEL_KEY uv run");
    await view?.unmount();

    const codex = e2bRuntime({
      type: "codex_api",
      details: { isolation: "e2b", template: CODEX_PIN },
      credential_names: ["OPENAI_API_KEY", "E2B_API_KEY"],
    });
    const codexRoot = await page(codex, [build({ pin: CODEX_PIN, verified: false })]);
    expect(codexRoot.textContent).toContain("OPENAI_API_KEY=YOUR_MODEL_KEY uv run");
    expect(codexRoot.textContent).not.toContain("ANTHROPIC_API_KEY=");
    await view?.unmount();

    const unlisted = await page(e2bRuntime(), []);
    expect(unlisted.textContent).toContain("isn't among the team's builds");
    await view?.unmount();

    const verified = await page(e2bRuntime(), [build({ verified: true })]);
    expect(verified.textContent).not.toContain(UNVERIFIED_NOTE);
    expect(verified.textContent).not.toContain("isn't among the team's builds");
  });
});

const PIN = "factorial-claude-2-1-284:a1b2c3d4";
const CODEX_PIN = "factorial-codex-0-159-0:a1b2c3d4";
const UNVERIFIED_NOTE = "This template hasn't passed the interactive test";

function build(overrides: Partial<RuntimeTemplateBuild>): RuntimeTemplateBuild {
  return {
    pin: PIN,
    template: "factorial-claude-2-1-284",
    build_id: "a1b2c3d4",
    created_at: AT,
    cpu_count: 2,
    memory_mb: 4096,
    matches_harness: true,
    verified: false,
    ...overrides,
  };
}

function button(root: HTMLElement, name: string): HTMLButtonElement | null {
  return (
    [...root.querySelectorAll<HTMLButtonElement>("button")].find(
      (each) => each.textContent?.trim() === name,
    ) ?? null
  );
}

function outcome(root: HTMLElement, title: string): string | null {
  const message = [...root.querySelectorAll("section[data-appearance]")].find(
    (each) => each.querySelector("h3")?.textContent === title,
  );
  return message?.getAttribute("data-appearance") ?? null;
}

describe("Test credential", () => {
  it("shows a check and the detail when the vendor accepts, and the E2B key's result", async () => {
    tested = {
      ok: true,
      detail: "Anthropic accepted the stored API key",
      local: false,
      e2b: { ok: false, detail: "E2B did not answer to the stored E2B_API_KEY" },
    };
    const root = await page(e2bRuntime());

    await click(button(root, "Test credential"));
    await settle();

    expect(root.textContent).toContain("Anthropic accepted the stored API key");
    expect(outcome(root, "Model provider")).toBe("success");
    expect(outcome(root, "Cloud sandbox")).toBe("error");
    expect(root.textContent).toContain("E2B did not answer to the stored E2B_API_KEY");
  });

  it("shows the detail of a refusal", async () => {
    tested = {
      ok: false,
      detail: "Anthropic refused the stored API key (401): store a fresh one",
      local: false,
      e2b: null,
    };
    const root = await page(e2bRuntime());

    await click(button(root, "Test credential"));
    await settle();

    expect(root.textContent).toContain(
      "Anthropic refused the stored API key (401): store a fresh one",
    );
    expect(outcome(root, "Model provider")).toBe("error");
  });

  it("names a codex subscription's check as local", async () => {
    tested = {
      ok: true,
      detail:
        "the stored login is unexpired until 2026-10-06 09:00 UTC (not checked against OpenAI)",
      local: true,
      e2b: null,
    };
    const root = await page(
      e2bRuntime({
        type: "codex_subscription",
        details: {},
        credential_names: ["CODEX_AUTH_JSON"],
        credentials: [{ name: "CODEX_AUTH_JSON", created_at: AT, updated_at: AT }],
      }),
    );
    expect(root.textContent).toContain("A local check");

    await click(button(root, "Test credential"));
    await settle();

    expect(root.textContent).toContain("Local check");
    expect(root.textContent).toContain("not checked against OpenAI");
  });

  it("drops a shown result once the credential it tested is replaced", async () => {
    tested = {
      ok: false,
      detail: "Anthropic refused the stored API key (401): store a fresh one",
      local: false,
      e2b: null,
    };
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    qc.setQueryData(keys.runtimeProjects(31), []);
    const at = (runtime: Runtime) => (
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <RuntimeDetail runtime={runtime} />
        </MemoryRouter>
      </QueryClientProvider>
    );
    view = await mount(at(e2bRuntime()));
    await click(button(view.container, "Test credential"));
    await settle();
    expect(view.container.textContent).toContain("(401)");

    const replaced = "2026-09-02T09:00:00Z";
    await view.rerender(
      at(
        e2bRuntime({
          credentials: [
            { name: "ANTHROPIC_API_KEY", created_at: AT, updated_at: replaced },
            { name: "E2B_API_KEY", created_at: AT, updated_at: AT },
          ],
        }),
      ),
    );
    await settle();

    expect(view.container.textContent).not.toContain("(401)");
  });

  it("shows the last time a run found the credential refused, with its ticket", async () => {
    const root = await page(
      e2bRuntime({
        last_auth_failure: {
          at: AT,
          reason: "API Error: 401 Invalid bearer token",
          ticket: "acme/app#42",
          ticket_id: 7,
        },
      }),
    );

    expect(root.textContent).toContain(
      "Last sign-in failure: the provider rejected the credential",
    );
    expect(root.querySelector("code[hidden]")?.textContent).toBe(
      "API Error: 401 Invalid bearer token",
    );
    const link = [...root.querySelectorAll("a")].find((a) => a.textContent === "acme/app#42");
    expect(link?.getAttribute("href")).toBe("/tickets/7");
  });
});

describe("the Factorial-managed sandbox key", () => {
  const managedRuntime = (overrides: Partial<Runtime> = {}) =>
    e2bRuntime({
      details: { isolation: "e2b", sandbox_key: "managed" },
      credential_names: ["ANTHROPIC_API_KEY"],
      credentials: [{ name: "ANTHROPIC_API_KEY", created_at: AT, updated_at: AT }],
      sandbox_key: "managed",
      template: "factorial-claude-2:abcd1234",
      ...overrides,
    });
  const deployment = { managed: ["claude_api@e2b"], limit: 5 };

  it("reads as a managed row with one action and a read-only template", async () => {
    const root = await page(managedRuntime(), undefined, [], deployment);
    expect(root.textContent).toContain("Provided by Factorial. 5 runs at a time.");
    expect(button(root, "Use your own key")).not.toBeNull();
    expect(root.textContent).toContain("Managed");
    expect(root.textContent).toContain("factorial-claude-2:abcd1234");
    expect(root.textContent).not.toContain("Needed to run");
    expect(button(root, "Change")).toBeNull();
    expect(button(root, "Use the Factorial-managed key")).toBeNull();
  });

  it("says where the sandboxes run: Factorial's account on the managed key, the key's team on an own key", async () => {
    const managed = await page(managedRuntime(), undefined, [], deployment);
    expect(managed.textContent).toContain(
      "Sandboxes run in Factorial's sandbox account, from a template Factorial provides.",
    );
    expect(managed.textContent).not.toContain("the team this key belongs to");
    await view?.unmount();
    view = null;
    const own = await page(e2bRuntime(), undefined, [], deployment);
    expect(own.textContent).toContain(
      "Sandboxes run in the team this key belongs to, from a template built in that team.",
    );
  });

  it("says one run in the singular, and names no limit when it is unknown", async () => {
    const one = await page(managedRuntime(), undefined, [], { ...deployment, limit: 1 });
    expect(one.textContent).toContain("Provided by Factorial. 1 run at a time.");
    await view?.unmount();
    view = null;
    const unknown = await page(managedRuntime(), undefined, [], { ...deployment, limit: null });
    expect(unknown.textContent).toContain("Provided by Factorial.");
    expect(unknown.textContent).not.toContain("at a time");
  });

  it("shows the sentence of a managed runtime that cannot start a sandbox", async () => {
    const root = await page(
      managedRuntime({
        template: null,
        sandbox_gap: "The managed key has no template for Claude.",
      }),
      undefined,
      [],
      deployment,
    );
    expect(root.textContent).toContain("The managed key has no template for Claude.");
    expect(button(root, "Use your own key")).not.toBeNull();
  });

  it("opens the key form behind Use your own key", async () => {
    const root = await page(managedRuntime(), undefined, [], deployment);
    expect(root.querySelector("#field-credential-e2b")).toBeNull();
    await click(button(root, "Use your own key"));
    expect(root.querySelector("#field-credential-e2b")).not.toBeNull();
  });

  it("offers the move to the managed key only to a covered own-key runtime, and sends the PATCH after the confirm", async () => {
    const uncovered = await page(e2bRuntime(), undefined, [], { managed: [], limit: 5 });
    expect(button(uncovered, "Use the Factorial-managed key")).toBeNull();
    await view?.unmount();
    const noLimit = await page(e2bRuntime(), undefined, [], { ...deployment, limit: 0 });
    expect(button(noLimit, "Use the Factorial-managed key")).toBeNull();
    await view?.unmount();

    const root = await page(e2bRuntime(), undefined, [], deployment);
    await click(button(root, "Use the Factorial-managed key"));
    expect(patches).toEqual([]);
    expect(document.body.textContent).toContain(
      "The runtime's own sandbox key and its template are removed. Sandboxes then run in Factorial's sandbox account, up to 5 runs at a time for this organization.",
    );
    await click(button(document.body, "Use the managed key"));
    await settle();
    expect(patches).toEqual([{ details: { isolation: "e2b", sandbox_key: "managed" } }]);
  });

  it("keeps the runtime's other details and sends no template", async () => {
    const root = await page(
      e2bRuntime({ details: { isolation: "e2b", template: "t:1", note: "kept" } }),
      undefined,
      [],
      deployment,
    );
    await click(button(root, "Use the Factorial-managed key"));
    await click(button(document.body, "Use the managed key"));
    await settle();
    expect(patches).toEqual([
      { details: { isolation: "e2b", note: "kept", sandbox_key: "managed" } },
    ]);
  });

  it("shows the API's sentence in the dialog when it refuses the move", async () => {
    patchRefusal = { status: 409, detail: "Runtime 'claude-e2b' has live step run(s) (1)." };
    const root = await page(e2bRuntime(), undefined, [], deployment);
    await click(button(root, "Use the Factorial-managed key"));
    await click(button(document.body, "Use the managed key"));
    await settle();
    expect(document.body.textContent).toContain("Runtime 'claude-e2b' has live step run(s) (1).");
    expect(button(document.body, "Use the managed key")).not.toBeNull();
    expect(button(root, "Use the Factorial-managed key")).not.toBeNull();
  });
});
