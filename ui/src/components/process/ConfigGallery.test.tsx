// @vitest-environment happy-dom
/**
 * The gallery a project's processes are imported from. Its writes belong to the section, so these
 * tests read what it offers and what it hands to `onImport`, never a request it sends.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { keys } from "../../api/keys";
import { ADMIN_PERMISSIONS, holding } from "../../api/organization.fixture";
import type { DefaultBranchConfig, ProcessTemplate, ProjectDetail, Runtime } from "../../api/types";
import { byText, choose, click, type Mounted, mount, settle } from "../../testDom";
import { ConfigGallery, type GalleryChoice, type GalleryMode } from "./ConfigGallery";

const PROJECT_ID = 9077;
const CONTENT = {
  entry: { step: "plan" },
  steps: [
    { name: "plan", kind: "agent", agent: "plan-agent", next: "gate" },
    { name: "gate", kind: "human_gate", next: "done" },
  ],
  terminal: { step: "done" },
};
const SIMPLE: ProcessTemplate = {
  key: "simple",
  title: "Simple lane",
  description: "Plan → spec review → done.",
  lanes: [{ key: "simple", yaml: "name: simple\n", content: CONTENT, agents: [] }],
};
const PAIR: ProcessTemplate = {
  key: "pair",
  title: "Feature and hotfix",
  description: "Two lanes.",
  lanes: [
    { key: "feature", yaml: "name: feature\n", content: CONTENT, agents: [] },
    { key: "hotfix", yaml: "name: hotfix\n", content: CONTENT, agents: [] },
  ],
};
const FROM_MAIN: DefaultBranchConfig = {
  branch: "main",
  sha: "9".repeat(40),
  config_dir: ".factorial",
  lanes: [{ key: "standard", content: CONTENT, agents: [] }],
  error: null,
};
const project = (lanes: string[] = [], permitted: string[] = []) =>
  ({
    id: PROJECT_ID,
    processes: lanes.map((process_key) => ({ process_key })),
    settings: { runtime_allowlist: permitted },
  }) as unknown as ProjectDetail;

const runtime = (slug: string, name = slug) =>
  ({ id: slug.length, slug, name, type: "claude_api", details: {} }) as unknown as Runtime;
const CLAUDE = runtime("claude-api", "Claude API");
const SANDBOX = { ...runtime("claude-on-e2b"), details: { isolation: "e2b" } } as Runtime;

let view: Mounted | null = null;
let client: QueryClient;
afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.unstubAllGlobals();
});

async function render(
  options: {
    mode?: GalleryMode;
    lanes?: string[];
    templates?: ProcessTemplate[] | "error";
    fromMain?: DefaultBranchConfig | null | "error" | "pending";
    runtimes?: Runtime[] | "error";
    permitted?: string[];
    permissions?: string[];
    busy?: boolean;
    onImport?: (choice: GalleryChoice) => void;
  } = {},
) {
  // Anything not seeded below is a request, and every request fails: that is how the error
  // states are reached.
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      options.fromMain === "pending"
        ? new Promise<Response>(() => {})
        : new Response(JSON.stringify({ detail: "boom" }), { status: 502 }),
    ),
  );
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client = qc;
  holding(qc, options.permissions ?? ADMIN_PERMISSIONS);
  if (options.runtimes !== "error") qc.setQueryData(keys.runtimes, options.runtimes ?? [CLAUDE]);
  if (options.templates !== "error") {
    qc.setQueryData(keys.processTemplates, { templates: options.templates ?? [SIMPLE] });
  }
  if (
    options.fromMain !== "error" &&
    options.fromMain !== "pending" &&
    options.fromMain !== undefined
  ) {
    qc.setQueryData(keys.defaultBranchConfig(PROJECT_ID), options.fromMain);
  }
  view = await mount(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ConfigGallery
          project={project(options.lanes, options.permitted)}
          mode={options.mode ?? "branch-missing"}
          busy={options.busy ?? false}
          error={null}
          onImport={options.onImport ?? (() => {})}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await settle();
}

const cards = () => [...document.querySelectorAll("[data-gallery-card]")];
const vacant = () => [...document.querySelectorAll("[data-gallery-vacant]")];
const lanes = () =>
  [...document.querySelectorAll('[role="tablist"][aria-label="Processes"] [role="tab"]')].map(
    (tab) => tab.textContent,
  );
const importButton = () => byText(document.body, "button", "Import process template");
const runtimeSelect = () =>
  document.querySelector<HTMLSelectElement>('select[aria-label="Runtime"]');
const reasonOf = (button: Element | null) =>
  button
    ?.getAttribute("aria-describedby")
    ?.split(" ")
    .map((id) => document.getElementById(id)?.textContent)
    .join(" ");

describe("ConfigGallery", () => {
  it("fills four slots: From main, the templates, then vacant slots no one can use", async () => {
    await render({ fromMain: FROM_MAIN });
    expect(cards().map((card) => card.textContent)).toEqual([
      expect.stringContaining("From main"),
      expect.stringContaining("Simple lane"),
    ]);
    expect(vacant()).toHaveLength(2);
    for (const slot of vacant()) {
      expect(slot.tagName).toBe("DIV");
      expect(slot.getAttribute("aria-hidden")).toBe("true");
      expect(slot.hasAttribute("tabindex")).toBe(false);
      expect(slot.getAttribute("role")).toBeNull();
      expect(slot.querySelector("button")).toBeNull();
    }
  });

  it("holds the From card's slot, saying so, while main's folder is still being read", async () => {
    await render({ fromMain: "pending" });
    expect(document.body.textContent).toContain("Loading the default branch");
    expect(cards()).toHaveLength(1);
    expect(vacant()).toHaveLength(2);
  });

  it("offers no From card when adding a lane", async () => {
    await render({ mode: "new-lane", fromMain: FROM_MAIN });
    expect(cards()).toHaveLength(1);
    expect(vacant()).toHaveLength(3);
  });

  it("offers no From card when main has no config folder", async () => {
    await render({ fromMain: null });
    expect(cards()).toHaveLength(1);
  });

  it("heads itself Import Process, and imports the one template picked", async () => {
    const onImport = vi.fn();
    await render({ mode: "new-lane", lanes: ["simple"], onImport });
    expect(document.querySelector("h3")?.textContent).toBe("Import process config");
    expect(cards()[0]?.textContent).toContain("2 steps");
    expect(cards()[0]?.textContent).toContain("1 gate");
    expect(document.querySelector("[class*='flow-graph']")).toBeNull();
    await click(cards()[0]);
    expect(document.querySelector("[class*='flow-graph']")).not.toBeNull();
    // No name to type: each lane takes the next free one.
    expect(document.querySelector("input")).toBeNull();
    await click(byText(document.body, "button", "Import process template"));
    expect(onImport).toHaveBeenCalledWith({
      kind: "template",
      title: "Simple lane",
      lanes: [{ name: "simple-2", yaml: "name: simple\n" }],
      runtime: "claude-api",
      permit: [],
    });
  });

  it("offers Import beside the heading, shut until a template is picked", async () => {
    await render({ mode: "new-lane" });
    const header = document.querySelector("h3")?.closest("div")?.parentElement;
    expect(header?.contains(importButton())).toBe(true);
    expect(importButton()?.hasAttribute("disabled")).toBe(true);
    await click(cards()[0]);
    expect(importButton()?.hasAttribute("disabled")).toBe(false);
  });

  it("picks one template at a time", async () => {
    await render({ mode: "new-lane", templates: [SIMPLE, PAIR] });
    await click(cards()[0]);
    await click(cards()[1]);
    expect(cards().map((card) => card.getAttribute("aria-pressed"))).toEqual(["false", "true"]);
  });

  it("lists every lane of a template, draws the one chosen, and imports them all", async () => {
    const onImport = vi.fn();
    await render({ mode: "new-lane", lanes: ["hotfix"], templates: [PAIR], onImport });
    expect(cards()[0]?.textContent).toContain("2 processes");
    await click(cards()[0]);
    const preview = document.querySelector("[data-gallery-preview]");
    expect(preview?.querySelector("h4")?.textContent).toBe("Feature and hotfix");
    expect(lanes()).toEqual(["feature", "hotfix"]);
    expect(
      [...document.querySelectorAll('[role="tab"] [data-glyph="lane"]')].map(
        (icon) => icon.parentElement?.textContent,
      ),
    ).toEqual(["feature", "hotfix"]);
    expect(preview?.querySelector('[role="tabpanel"]')?.textContent).toContain("process · feature");
    await click(byText(document.body, '[role="tab"]', "hotfix"));
    expect(document.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toBe(
      "hotfix",
    );
    expect(preview?.querySelector('[role="tabpanel"]')?.textContent).toContain("process · hotfix");

    await click(byText(document.body, "button", "Import process template"));
    expect(onImport).toHaveBeenCalledWith({
      kind: "template",
      title: "Feature and hotfix",
      lanes: [
        { name: "feature", yaml: "name: feature\n" },
        { name: "hotfix-2", yaml: "name: hotfix\n" },
      ],
      runtime: "claude-api",
      permit: [],
    });
  });

  it("copies main's folder, even when it does not parse", async () => {
    const onImport = vi.fn();
    await render({ fromMain: { ...FROM_MAIN, lanes: [], error: "process.yaml: bad" }, onImport });
    expect(cards()[0]?.textContent).toContain("process.yaml: bad");
    await click(cards()[0]);
    await click(byText(document.body, "button", "Import main's config"));
    expect(onImport).toHaveBeenCalledWith({ kind: "default-branch" });
  });

  it("asks for a runtime beside Import once a template is picked, naming each by name and slug", async () => {
    await render({ mode: "new-lane", runtimes: [CLAUDE, SANDBOX] });
    expect(runtimeSelect()).toBeNull();
    await click(cards()[0]);
    const header = document.querySelector("h3")?.closest("div")?.parentElement;
    expect(header?.contains(runtimeSelect())).toBe(true);
    expect([...(runtimeSelect()?.options ?? [])].map((option) => option.textContent)).toEqual([
      "Pick a runtime",
      "Claude API · claude-api",
      "claude-on-e2b",
    ]);
  });

  it("keeps Import shut, saying why, until a runtime is picked", async () => {
    const onImport = vi.fn();
    await render({ mode: "new-lane", runtimes: [CLAUDE, SANDBOX], onImport });
    await click(cards()[0]);
    expect(runtimeSelect()?.value).toBe("");
    expect(importButton()?.getAttribute("aria-disabled")).toBe("true");
    expect(reasonOf(importButton())).toBe("Pick a runtime first");
    await click(importButton());
    expect(onImport).not.toHaveBeenCalled();

    await choose(runtimeSelect(), "claude-api");
    expect(importButton()?.hasAttribute("aria-disabled")).toBe(false);
    await click(importButton());
    expect(onImport).toHaveBeenCalledWith(
      expect.objectContaining({ runtime: "claude-api", permit: [] }),
    );
  });

  it("names the permit on the button when the runtime picked needs one", async () => {
    const onImport = vi.fn();
    await render({ mode: "new-lane", runtimes: [CLAUDE, SANDBOX], onImport });
    await click(cards()[0]);
    await choose(runtimeSelect(), "claude-on-e2b");
    expect(importButton()).toBeNull();
    expect(document.body.textContent).toContain(
      "The import also permits claude-on-e2b for this project.",
    );
    await click(byText(document.body, "button", "Permit claude-on-e2b and import"));
    expect(onImport).toHaveBeenCalledWith(
      expect.objectContaining({ runtime: "claude-on-e2b", permit: ["claude-on-e2b"] }),
    );
  });

  it("forgets a pick whose runtime has since gone", async () => {
    const onImport = vi.fn();
    await render({ mode: "new-lane", runtimes: [CLAUDE, SANDBOX], onImport });
    await click(cards()[0]);
    await choose(runtimeSelect(), "claude-api");
    client.setQueryData(keys.runtimes, [SANDBOX, runtime("codex-api")]);
    await settle();
    expect(runtimeSelect()?.value).toBe("");
    expect(reasonOf(importButton())).toBe("Pick a runtime first");
    await click(importButton());
    expect(onImport).not.toHaveBeenCalled();
  });

  it("holds the runtime still while the import runs", async () => {
    await render({ mode: "new-lane", busy: true });
    await click(cards()[0]);
    expect(runtimeSelect()?.disabled).toBe(true);
  });

  it("says once that the runtimes failed to load, and Import points at it", async () => {
    await render({ mode: "new-lane", runtimes: "error" });
    await click(cards()[0]);
    expect(document.body.textContent?.split("boom")).toHaveLength(2);
    expect(importButton()?.getAttribute("aria-disabled")).toBe("true");
    expect(reasonOf(importButton())).toContain("boom");
  });

  it("offers a reader who cannot list runtimes the ones the project permits, by slug", async () => {
    const onImport = vi.fn();
    await render({
      mode: "new-lane",
      runtimes: [],
      permitted: ["claude-on-e2b"],
      permissions: ADMIN_PERMISSIONS.filter((key) => key !== "runtimes:manage"),
      onImport,
    });
    await click(cards()[0]);
    expect([...(runtimeSelect()?.options ?? [])].map((option) => option.textContent)).toEqual([
      "Pick a runtime",
      "claude-on-e2b",
    ]);
    expect(runtimeSelect()?.value).toBe("claude-on-e2b");
    await click(importButton());
    expect(onImport).toHaveBeenCalledWith(
      expect.objectContaining({ runtime: "claude-on-e2b", permit: [] }),
    );
  });

  it("picks the one runtime the project permits", async () => {
    await render({ mode: "new-lane", runtimes: [CLAUDE, SANDBOX], permitted: ["claude-on-e2b"] });
    await click(cards()[0]);
    expect(runtimeSelect()?.value).toBe("claude-on-e2b");
  });

  it("ignores a permit whose runtime is gone, and picks the organization's only runtime", async () => {
    await render({ mode: "new-lane", runtimes: [CLAUDE], permitted: ["deleted-runtime"] });
    await click(cards()[0]);
    expect(runtimeSelect()?.value).toBe("claude-api");
    expect(importButton()?.hasAttribute("aria-disabled")).toBe(false);
  });

  it("picks nothing when the project permits several and the organization has several", async () => {
    await render({
      mode: "new-lane",
      runtimes: [CLAUDE, SANDBOX],
      permitted: ["claude-api", "claude-on-e2b"],
    });
    await click(cards()[0]);
    expect(runtimeSelect()?.value).toBe("");
  });

  it("with no runtime at all, links to creating one", async () => {
    await render({ mode: "new-lane", runtimes: [] });
    await click(cards()[0]);
    expect(runtimeSelect()).toBeNull();
    expect(importButton()?.getAttribute("aria-disabled")).toBe("true");
    expect(reasonOf(importButton())).toBe("No runtime yet. Create one first.");
    expect(byText(document.body, "a", "Create a runtime")?.getAttribute("href")).toBe(
      "/runtimes/new",
    );
  });

  it("with no runtime, tells a reader who cannot create one whom to ask", async () => {
    await render({
      mode: "new-lane",
      runtimes: [],
      permissions: ADMIN_PERMISSIONS.filter((key) => key !== "runtimes:manage"),
    });
    await click(cards()[0]);
    expect(runtimeSelect()).toBeNull();
    expect(byText(document.body, "a", "Create a runtime")).toBeNull();
    expect(reasonOf(importButton())).toBe(
      "No runtime yet. Ask an organization admin to create one.",
    );
  });

  it("copies main's folder without asking for a runtime: its files carry their own", async () => {
    const onImport = vi.fn();
    await render({ runtimes: [], fromMain: FROM_MAIN, onImport });
    await click(cards()[0]);
    expect(runtimeSelect()).toBeNull();
    const copy = byText(document.body, "button", "Import main's config");
    expect(copy?.hasAttribute("aria-disabled")).toBe(false);
    await click(copy);
    expect(onImport).toHaveBeenCalledWith({ kind: "default-branch" });
  });

  it("says the templates failed to load rather than drawing four empty slots", async () => {
    await render({ templates: "error", fromMain: null });
    expect(document.body.textContent).toContain("boom");
    expect(vacant()).toHaveLength(0);
  });

  it("keeps the templates usable when main's preview fails", async () => {
    await render({ fromMain: "error" });
    expect(document.body.textContent).toContain("boom");
    expect(cards()).toHaveLength(1);
    expect(cards()[0]?.textContent).toContain("Simple lane");
  });
});
