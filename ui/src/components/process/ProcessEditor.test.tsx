// @vitest-environment happy-dom
/**
 * The process-config section's structural invariants: what is on top, what is behind a drawer,
 * and what the version table says about where each version came from.
 *
 * Mostly rendered with `renderToString` against a pre-seeded query cache, the way
 * `TicketPage.test.tsx` does it: no fetch, and the data is there synchronously so the section
 * renders its real content instead of a spinner. Effects do not run, which is what makes the
 * FIRST paint the thing under test. The pull-request gate is the exception: it only closes once
 * the editor has seeded its text and the server has validated it, so those tests mount
 * (decision 51) with `fetch` answering the one request that is not in the cache.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { keys } from "../../api/keys";
import { ADMIN_PERMISSIONS, holding, MEMBER_PERMISSIONS } from "../../api/organization.fixture";
import type {
  ConfigCommitResult,
  ConfigEdit,
  ConfigFile,
  ConfigPreview,
  EditorDraftPayload,
  FileProposal,
  ProcessVersion,
  ProjectDetail,
  ReadinessItem,
  Runtime,
  SelectedFile,
} from "../../api/types";
import { byText, choose, click, mount, pointerDown, settle, type, typeAt } from "../../testDom";
import { clearFlags, FlagGroup } from "../ui/Flag";
import { EditorMessages } from "./EditorMessages";
import { readBundleDraft, writeBundleDraft } from "./explorer/bundleDraft";
import { readCreatedFiles } from "./explorer/createdFiles";
import { readDeletedFiles, writeDeletedFiles } from "./explorer/deletedFiles";
import { readRepoEdits, writeRepoEdits } from "./explorer/repoEdits";
import { ProcessEditorBody } from "./ProcessEditorBody";
import { ProcessSection } from "./ProcessSection";
import { writeDraft } from "./processDraft";

const PROJECT_ID = 9041;

const CONFIG = {
  entry: { step: "plan" },
  steps: [
    { name: "plan", kind: "agent", next: "spec_gate" },
    { name: "spec_gate", kind: "human_gate", next: "done" },
  ],
  terminal: { step: "done" },
};

const PROCESS_KEY = "standard";

function snapshot(id: number, sha: string, source_ref: string | null): ProcessVersion {
  return {
    id,
    project_id: PROJECT_ID,
    process_key: PROCESS_KEY,
    source_repo: "acme/widgets",
    source_commit_sha: sha,
    retired_at: null,
    source_ref,
    adopted_by: null,
    activated_at: "2026-08-25T09:00:00Z",
    created_at: "2026-08-25T09:00:00Z",
  };
}

const CURRENT_SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0";
const LATEST = snapshot(30, CURRENT_SHA, "factorial");
/** The row every snapshot route and cache key addresses. */
const LATEST_ID = LATEST.id;

const PROJECT = {
  id: PROJECT_ID,
  name: "Harbor invoices",
  github_repo: "northwind/harbor-invoices",
  description: null,
  active: true,
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:00:00Z",
  ticket_source: null,
  processes: [
    {
      process_key: PROCESS_KEY,
      is_default: true,
      selection_label: `factorial:process:${PROCESS_KEY}`,
      latest: LATEST,
    },
  ],
  default_process: PROCESS_KEY,
  ticket_count: 0,
  open_ticket_count: 0,
  secret_names: [],
  blockers: [],
  warnings: [],
  readiness: [],
} as unknown as ProjectDetail;

/** `project` overrides the fixture; the cache is seeded either way, since the graph does not
 *  render at all without its snapshot and every assertion here is about what sits around it. */
function render(
  project: ProjectDetail = PROJECT,
  branch?: ReadinessItem,
  permissions = ADMIN_PERMISSIONS,
): string {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  holding(qc, permissions);
  qc.setQueryData(keys.processTemplates, { templates: [SIMPLE_TEMPLATE] });
  qc.setQueryData(keys.processVersion(PROJECT_ID, LATEST_ID), {
    project_id: PROJECT_ID,
    process_key: PROCESS_KEY,
    source_commit_sha: CURRENT_SHA,
    content: CONFIG,
  });
  qc.setQueryData(keys.processVersionRaw(PROJECT_ID, LATEST_ID), {
    content: "specVersion: adlc/v1\nname: standard\n",
    source: "repo",
  });
  return renderToString(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ProcessSection project={project} branch={branch} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The `config_branch` probe row, whose `missing` and `unknown head` markers the section keys on. */
function branchItem(
  state: ReadinessItem["state"],
  summary: string,
  extra: Partial<ReadinessItem> = {},
): ReadinessItem {
  return {
    key: "config_branch",
    process: null,
    runtime_slug: null,
    state,
    summary,
    detail: null,
    remedy: null,
    link: null,
    ...extra,
  };
}

const NO_LANES = {
  ...PROJECT,
  settings: { runtime_allowlist: [] },
  processes: [],
  default_process: null,
} as unknown as ProjectDetail;

describe("ProcessSection", () => {
  it("draws a lane for a reader without projects:manage, with nothing that changes it but View config", () => {
    const lane = { ...PROJECT.processes[0], is_default: false };
    const html = render(
      { ...PROJECT, processes: [lane], default_process: "hotfix" } as ProjectDetail,
      branchItem("warn", "unknown head", { remedy: "git push --force factory abc:factorial" }),
      MEMBER_PERMISSIONS,
    );
    expect(html).toContain("flow-graph");
    expect(html).toMatch(/Config is fetched from the <code[^>]*>factorial<\/code> branch\./);
    expect(html).toContain("Command to reset the factorial branch");
    // The editor opens for them too, read-only.
    expect(html).toContain("View config");
    for (const write of [
      "Edit config",
      "Sync config",
      "Review and sync",
      "Review and adopt",
      "Set as default",
    ]) {
      expect(html).not.toContain(write);
    }
  });

  it("stays on the process it was opened on when the project's processes change under it", async () => {
    stubEditorApi();
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    holding(qc);
    qc.setQueryData(keys.processTemplates, { templates: [SIMPLE_TEMPLATE] });
    qc.setQueryData(keys.runtimes, RUNTIMES);
    for (const id of [LATEST_ID, 31]) {
      qc.setQueryData(keys.processVersion(PROJECT_ID, id), {
        project_id: PROJECT_ID,
        process_key: id === 31 ? "alpha" : PROCESS_KEY,
        source_commit_sha: CURRENT_SHA,
        content: CONFIG,
      });
      qc.setQueryData(keys.processVersionRaw(PROJECT_ID, id), {
        content: `specVersion: adlc/v1\nname: ${id === 31 ? "alpha" : PROCESS_KEY}\n`,
        source: "repo",
      });
    }
    const alpha = {
      process_key: "alpha",
      is_default: false,
      selection_label: "factorial:process:alpha",
      latest: { ...snapshot(31, CURRENT_SHA, "factorial"), process_key: "alpha" },
    };
    const section = (project: ProjectDetail) => (
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ProcessSection project={project} />
        </MemoryRouter>
      </QueryClientProvider>
    );
    const settings = { runtime_allowlist: [] };
    const both = {
      ...PROJECT,
      settings,
      processes: [alpha, ...PROJECT.processes],
    } as ProjectDetail;
    const view = await mount(section(both));
    await click(byText(document.body, "button", "Edit config"));
    await settle();
    const heading = () =>
      [...document.querySelectorAll("h2")].find((h) => h.textContent?.startsWith("Edit config"));
    expect(heading()?.textContent).toBe(`Edit config · ${PROCESS_KEY}`);
    // The process it opened on is retired by a commit: the section's own pick moves to alpha.
    await view.rerender(
      section({
        ...PROJECT,
        settings,
        processes: [alpha],
        default_process: null,
      } as ProjectDetail),
    );
    await settle();
    expect(heading()?.textContent).toBe(`Edit config · ${PROCESS_KEY}`);
    await view.unmount();
  });

  it("offers History to a manager and not to a reader", () => {
    expect(render()).toMatch(/<button[^>]*>(<[^>]+>)*History/);
    expect(render(PROJECT, undefined, MEMBER_PERMISSIONS)).not.toContain("History");
  });

  it("shows a reader no gallery when the branch is missing and nothing is synced", () => {
    const html = render(NO_LANES, branchItem("warn", "missing"), MEMBER_PERMISSIONS);
    expect(html).not.toContain("data-gallery-vacant");
    expect(html).not.toContain("Create the branch");
    expect(html).toContain("No process yet.");
    expect(html).not.toContain("Press Edit config");
  });

  it("opens on the gallery when the branch is missing and nothing is synced", () => {
    const html = render(NO_LANES, branchItem("warn", "missing"));
    expect(html).toContain("data-gallery-vacant");
    expect(html).not.toContain("Sync config");
    expect(html).not.toContain("Edit config");
  });

  it("keeps a synced lane on screen when the branch is missing, with a way to create it", () => {
    const html = render(PROJECT, branchItem("warn", "missing"));
    expect(html).toContain("flow-graph");
    expect(html).toContain("Create the branch");
    expect(html).toMatch(/aria-disabled="true"[^>]*>(?:<[^>]+>)*Edit config/);
  });

  it("offers Sync config and Edit config, and no New lane", () => {
    const html = render(PROJECT, branchItem("ok", "factorial · no force-push or deletion"));
    expect(html).toContain("Sync config");
    expect(html).toContain("Edit config");
    expect(html).not.toContain("New lane");
    expect(html).not.toContain("Show config");
  });

  it("says how to reset the branch at an unknown head", () => {
    const head = render(
      PROJECT,
      branchItem("warn", "unknown head eeeeeee", { remedy: "git push --force-with-lease" }),
    );
    expect(head).toContain("Command to reset the factorial branch");
  });

  it("says a lane's start is waiting to be finished, once one has been drafted", () => {
    const ok = branchItem("ok", "factorial · no force-push or deletion");
    expect(render(NO_LANES, ok)).not.toContain("Unsaved edits");
    writeDraft(PROJECT_ID, null, null, "name: simple\n");
    const html = render(NO_LANES, ok);
    expect(html).toContain("Unsaved edits");
    expect(html).toContain("Your draft is waiting");
  });

  it("offers to finish a drafted new lane beside the lanes that already run", () => {
    writeDraft(PROJECT_ID, null, null, "name: hotfix\n");
    const html = render(PROJECT, branchItem("ok", "factorial · no force-push or deletion"));
    expect(html).toContain("Edit and finalize new process");
    expect(html).toContain("Edit config");
  });

  it("says an edited lane has unsaved edits, and makes Edit config the primary action", () => {
    writeDraft(PROJECT_ID, PROCESS_KEY, CURRENT_SHA, "name: standard\n# edited\n");
    const html = render(PROJECT, branchItem("ok", "factorial · no force-push or deletion"));
    expect(html).toContain("Unsaved edits");
    expect(html).toMatch(/data-appearance="primary"[^>]*>(?:<[^>]+>)*Edit config/);
  });

  it("holds the editor shut at an unknown head, saying why", () => {
    const html = render(NO_LANES, branchItem("warn", "unknown head eeeeeee"));
    expect(html).toMatch(/aria-disabled="true"[^>]*>(?:<[^>]+>)*Edit config/);
    expect(html).toContain("Adopt the head first");
  });

  it("holds an edited lane's editor shut while the branch is missing", () => {
    writeDraft(PROJECT_ID, PROCESS_KEY, CURRENT_SHA, "name: standard\n# edited\n");
    const html = render(PROJECT, branchItem("warn", "missing"));
    expect(html).toMatch(/aria-disabled="true"[^>]*>(?:<[^>]+>)*Edit config/);
    expect(html).toContain("Create the branch first");
  });

  it("puts the actions above the graph", () => {
    const html = render();
    expect(html).toContain("flow-graph");
    expect(html.indexOf("Sync config")).toBeLessThan(html.indexOf("flow-graph"));
    expect(html.indexOf("Edit config")).toBeLessThan(html.indexOf("flow-graph"));
  });

  it("does not advertise an uncommitted draft on the page", () => {
    expect(render()).not.toContain("Uncommitted draft");
  });

  it("keeps the file in the editor rather than on the page", () => {
    // The YAML is the longest thing in this section by an order of magnitude, and the raw text
    // is not fetched until the editor asks.
    const html = render();
    expect(html).toContain("a1b2c3d");
    expect(html).not.toContain("specVersion: adlc/v1"); // seeded, and deliberately not rendered
    expect(html).not.toContain("<textarea");
  });

  it("says the config comes from the factorial branch, with no branch to choose", () => {
    const html = render();
    expect(html).toMatch(/Config is fetched from the <code[^>]*>factorial<\/code> branch\./);
    expect(html).not.toContain("Sync from branch");
    expect(html).not.toContain("<input");
    expect(html).not.toContain("<table");
  });

  it("draws one tab per lane, the default one selected and marked", () => {
    const html = render({
      ...PROJECT,
      processes: [
        ...PROJECT.processes,
        {
          process_key: "hotfix",
          is_default: false,
          selection_label: "factorial:process:hotfix",
          latest: { ...LATEST, id: 31, process_key: "hotfix" },
        },
      ],
    });
    const tabs = html.match(/<button[^>]*role="tab"[^>]*>(?:(?!<\/button>).)*<\/button>/g) ?? [];
    expect(tabs).toHaveLength(2);
    expect(tabs[0]).toContain('aria-selected="true"');
    expect(tabs[0]).toContain(PROCESS_KEY);
    expect(tabs[0]).toContain(">default<");
    expect(tabs[0]).toContain('data-glyph="lane"');
    expect(html).not.toContain(">Lanes<");
    expect(tabs[1]).toContain('aria-selected="false"');
    expect(tabs[1]).not.toContain(">default<");
    // The lane's graph is inside its tab's panel.
    expect(html.indexOf('role="tabpanel"')).toBeLessThan(html.indexOf("flow-graph"));
  });

  it("puts Edit config and Sync config in the slot the page gives it, not over the lanes", async () => {
    const slot = document.createElement("div");
    document.body.append(slot);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    holding(qc);
    const view = await mount(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ProcessSection
            project={PROJECT}
            branch={branchItem("ok", "factorial · no force-push or deletion")}
            actionsSlot={slot}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(byText(slot, "button", "Edit config")).not.toBeNull();
    expect(byText(slot, "button", "Sync config")).not.toBeNull();
    expect(byText(view.container, "button", "Edit config")).toBeNull();
    // The lane tabs are the top of the section.
    expect(view.container.querySelector('[role="tablist"]')).not.toBeNull();
    await view.unmount();
    slot.remove();
  });

  it("puts Set as default in the stage's bottom-right, and only for a lane that is not", () => {
    // Promoting a lane acts on the one being drawn, so it belongs on the drawing, opposite the
    // facts.
    expect(render()).not.toContain("Set as default"); // the fixture's lane already is

    const html = render({
      ...PROJECT,
      processes: [
        {
          process_key: PROCESS_KEY,
          is_default: false,
          selection_label: `factorial:process:${PROCESS_KEY}`,
          latest: LATEST,
        },
      ],
      default_process: "hotfix",
    });
    const corner = html.indexOf("flow-process__footer-end");
    expect(corner).toBeGreaterThan(-1);
    expect(html.indexOf("Set as default")).toBeGreaterThan(corner);
  });

  it("drops a failed sync once a later snapshot is what the stage shows", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    holding(qc);
    qc.setQueryData(keys.processVersion(PROJECT_ID, LATEST_ID), {
      project_id: PROJECT_ID,
      process_key: PROCESS_KEY,
      source_commit_sha: CURRENT_SHA,
      content: CONFIG,
    });
    const message = "process config points at missing file";
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ detail: message }, 422)),
    );
    const tree = (project: ProjectDetail) => (
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ProcessSection project={project} />
        </MemoryRouter>
      </QueryClientProvider>
    );
    const view = await mount(tree(PROJECT));
    await click(byText(view.container, "button", "Sync config"));
    await settle();
    expect(view.container.textContent).toContain(message);

    const nextSha = "b".repeat(40);
    const next = snapshot(31, nextSha, "factorial");
    qc.setQueryData(keys.processVersion(PROJECT_ID, next.id), {
      project_id: PROJECT_ID,
      process_key: PROCESS_KEY,
      source_commit_sha: nextSha,
      content: CONFIG,
    });
    await view.rerender(
      tree({
        ...PROJECT,
        processes: [
          {
            process_key: PROCESS_KEY,
            is_default: true,
            selection_label: `factorial:process:${PROCESS_KEY}`,
            latest: next,
          },
        ],
      }),
    );
    expect(view.container.textContent).not.toContain(message);
    await view.unmount();
  });

  it("still offers a sync when the project has no config, because the repo may", () => {
    const html = render({ ...PROJECT, processes: [], default_process: null });
    expect(html).toContain("Sync config");
    expect(html).toContain("Edit config");
    expect(html).not.toContain('role="tab"');
  });
});

/**
 * The pull-request button's stale-file gate: `ProcessEditorBody` rendered directly (as
 * `ScaffoldChoices`/`ScaffoldedAgents` used to be), with the explorer's own state seeded into
 * the query cache the same way `ProcessVersion`/`ProjectDetail` are above — there is no DOM for
 * an effect to populate that state under `renderToString`, so anything the first paint has to
 * show must already be in the cache before the component renders.
 *
 * `text` stays empty in this harness (the baseline-seeding effect never runs), so the preview
 * query is seeded under the key it would use FOR empty text; `usePreviewConfigBundle`'s `enabled` flag
 * does not matter here — a query still returns pre-seeded cache data on its first render whether
 * or not `enabled` would have triggered a fetch of its own.
 */
const INSTRUCTIONS = ".factorial/agents/plan-agent/instructions.md";

function runtimeRow(slug: string, type: Runtime["type"]): Runtime {
  return {
    id: slug.length,
    slug,
    name: slug,
    type,
    details: {},
    metadata: {},
    credential_names: [],
    credentials: [],
    connected_projects: 0,
    last_auth_failure: null,
    sandbox_key: "own",
    template: null,
    sandbox_gap: null,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
  };
}

const RUNTIMES = [runtimeRow("claude-api", "claude_api"), runtimeRow("codex-prod", "codex_api")];

const RAW_TEXT = "specVersion: adlc/v1\nname: standard\n";

/** Keeps its `runtime:` commented out, as the shipped template does; the select writes it. */
const SIMPLE_LANE = {
  key: "simple",
  yaml: "# The Simple lane.\nspecVersion: factorial/v1\nkind: Process\nname: simple\n# runtime: claude-api\n",
  content: CONFIG,
  agents: [],
};
const SIMPLE_TEMPLATE = {
  key: "simple",
  title: "Simple lane",
  description: "Plan → spec review → build → test → code review → done, all on main.",
  lanes: [SIMPLE_LANE],
};

const SECOND = ".factorial/agents/plan-agent/system-prompt.md";

/** The shells the templates route serves, cut down to what the tests read. */
const SHELLS = {
  agent: {
    "agent.yaml": "kind: Agent\nname: __NAME__\n",
    "system-prompt.md": "You are `__NAME__`.\n",
    "instructions.md": "Instructions for `__NAME__`.\n",
  },
  skill: { "SKILL.md": "---\nname: __NAME__\n---\n" },
  mcp: { "server.yaml": "kind: McpServer\nname: __NAME__\n" },
  process: { "process.yaml": "kind: Process\nname: __NAME__\n# runtime: your-runtime\n" },
};

function proposal(path: string, body: string, facts: Record<string, string> = {}): FileProposal {
  return {
    path,
    body,
    component: "plan-agent",
    kind: "agent",
    variant: "base-target",
    version: 1,
    facts,
    present_in_repo: false,
  };
}

// Its fact differs from the stale draft's recorded one in `sectionTree`, which is what
// `reconcile` reads as "a marker this file used has moved since the edit".
const INSTRUCTIONS_PROPOSAL = proposal(INSTRUCTIONS, "new template body", {
  __BRANCH_PREFIX__: "release",
});

function sectionTree({
  stale,
  runtime = null,
  edited,
  proposals = [INSTRUCTIONS_PROPOSAL],
  components = [],
  repoFiles = [],
  fileBodies = {},
  laneless = false,
  lanes = true,
  runtimes = RUNTIMES,
  templates = [SIMPLE_TEMPLATE],
  allowlist = [],
  initialText,
  initialTitle,
  initialFile,
  initialDraft,
  onBack,
  pinnedUnread = false,
}: {
  stale: string[];
  /** The templates the editor's Import menu offers. */
  templates?: (typeof SIMPLE_TEMPLATE)[];
  /** An assistant's draft the editor opens on. */
  initialDraft?: EditorDraftPayload;
  /** The editor's way back, when a test watches for the editor closing itself. */
  onBack?: () => void;
  /** The pinned file and the listing have not been read yet. */
  pinnedUnread?: boolean;
  /** A gallery pick the editor starts on. */
  initialText?: string;
  initialTitle?: string;
  /** The file the editor opens on, under the config folder. */
  initialFile?: string;
  runtimes?: Runtime[];
  /** The project's `runtime_allowlist`. */
  allowlist?: string[];
  /** What the pinned version's config names, the select's seed under this harness. */
  runtime?: string | null;
  /** A text the test will type in, whose preview is seeded too so the explorer stays put. */
  edited?: string;
  proposals?: FileProposal[];
  /** What the preview says about each agent, skill and server the config names. */
  components?: ConfigPreview["components"];
  /** Files the pinned listing already has, after the two every version carries. */
  repoFiles?: ConfigFile[];
  /** What the repo holds at the pinned commit for each of those, by path. */
  fileBodies?: Record<string, string>;
  /** A lane that does not exist yet: no pinned version of its own. */
  laneless?: boolean;
  /** Whether the project has a synced lane at all; without one, nothing is listed. */
  lanes?: boolean;
}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  holding(qc);
  qc.setQueryData(keys.runtimes, runtimes);
  qc.setQueryData(keys.processTemplates, { templates, shells: SHELLS });
  qc.setQueryData(keys.processVersion(PROJECT_ID, LATEST_ID), {
    project_id: PROJECT_ID,
    version: 3,
    content: { ...CONFIG, runtime },
  });
  if (!pinnedUnread) {
    qc.setQueryData(keys.processVersionRaw(PROJECT_ID, LATEST_ID), {
      content: RAW_TEXT,
      source: "repo",
    });
  }

  const preview: ConfigPreview = { files: proposals, components };
  qc.setQueryData(keys.configPreview(PROJECT_ID, ""), preview);
  // And under the text a mounted editor seeds itself with, so the preview is there the moment
  // the text validates, without a request of its own.
  qc.setQueryData(keys.configPreview(PROJECT_ID, RAW_TEXT), preview);
  if (edited) qc.setQueryData(keys.configPreview(PROJECT_ID, edited), preview);

  if (!pinnedUnread)
    qc.setQueryData(keys.configFiles(PROJECT_ID, LATEST_ID), {
      config_dir: ".factorial",
      pinned_sha: "c".repeat(40),
      head_sha: "d".repeat(40),
      files: [
        {
          path: ".factorial/process.yaml",
          kind: "process",
          drift: "same",
          warning: null,
          sha: "a".repeat(40),
        },
        {
          path: ".factorial/agents/plan-agent/agent.yaml",
          kind: "agent",
          drift: "same",
          warning: "plan-agent: empty document",
          sha: "a".repeat(40),
        },
        ...repoFiles,
      ],
    });
  for (const [path, content] of Object.entries(fileBodies)) {
    qc.setQueryData(keys.configFile(PROJECT_ID, LATEST_ID, path), { path, content });
  }

  writeBundleDraft(
    PROJECT_ID,
    PROCESS_KEY,
    Object.fromEntries(
      stale.map((path) => [path, { body: "my own wording", facts: { __BRANCH_PREFIX__: "spec" } }]),
    ),
  );

  return (
    <QueryClientProvider client={qc}>
      <ProcessEditorBody
        project={{
          ...PROJECT,
          settings: { runtime_allowlist: allowlist },
          ...(lanes ? {} : { processes: [], default_process: null }),
        }}
        processKey={laneless ? null : PROCESS_KEY}
        initialText={initialText}
        initialTitle={initialTitle}
        initialFile={initialFile}
        initialDraft={initialDraft}
        heading={onBack ? { title: "Edit config", subtitle: "", onBack } : undefined}
      />
    </QueryClientProvider>
  );
}

function renderSection(options: Parameters<typeof sectionTree>[0]): string {
  return renderToString(sectionTree(options));
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** The API for a mounted editor: validation answers valid, `preview` answers config-preview (a
 *  promise that never settles leaves it loading), commits are recorded and answered by `commit`,
 *  and anything else is refused here rather than sent to a real network. */
function stubEditorApi({
  preview,
  commit,
  check,
  parsed = CONFIG,
}: {
  /** What the server reads the text as, when a test needs a name other than the fixture's. */
  parsed?: Record<string, unknown>;
  preview?: () => Promise<Response>;
  commit?: () => Response | Promise<Response>;
  /** Answers config-checks, given the draft it was sent. */
  check?: (draft: unknown) => Response;
} = {}) {
  const commits: Array<{ content: string; files: SelectedFile[]; edits?: ConfigEdit[] }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/process-versions/validate"))
        return jsonResponse({ valid: true, errors: [], warnings: [], content: parsed });
      if (url.endsWith("/config-preview") && preview) return preview();
      if (url.endsWith("/config-checks") && check) return check(JSON.parse(String(init?.body)));
      if (url.endsWith("/config-commits") && commit) {
        commits.push(JSON.parse(String(init?.body)));
        return await commit();
      }
      return jsonResponse({ detail: "not part of this test" }, 404);
    }),
  );
  return commits;
}

/** Opens every folder in the explorer, which opens on its top level only. */
async function expandTree() {
  for (let guard = 0; guard < 50; guard += 1) {
    const closed = document.querySelector('[aria-label^="Expand "]');
    if (!closed) return;
    await click(closed);
  }
}

/** The explorer's row for a file, with every folder opened to reach it. */
async function treeFile(name: string): Promise<HTMLElement | null> {
  await expandTree();
  return byText(document.body, "button", name);
}

function textareaFor(path: string): HTMLTextAreaElement | null {
  return document.querySelector<HTMLTextAreaElement>(`textarea[aria-label="${path}"]`);
}

/** The editor's Import menu opened, and its Simple lane item. */
async function openTemplateItem(): Promise<Element | undefined> {
  await click(byText(document.body, "button", "Import"));
  return [...document.querySelectorAll('[role="menuitem"]')].find((element) =>
    element.textContent?.startsWith("Simple lane"),
  );
}

/** The open-file tab whose file name starts with `name`. */
function fileTab(name: string): HTMLElement | null {
  return (
    [...document.querySelectorAll<HTMLElement>('[aria-label="Open files"] [role="tab"]')].find(
      (tab) => tab.textContent?.startsWith(name),
    ) ?? null
  );
}

/** Gives the process file a runtime line in its own tab, as the operator now does by typing,
 *  then goes back to the tab that was on show. */
async function typeRuntime(slug: string) {
  const back = document.querySelector(
    '[aria-label="Open files"] [role="tab"][aria-selected="true"]',
  );
  await click(fileTab("process-"));
  const area = textareaFor("process.yaml");
  await type(area, `${area?.value ?? ""}runtime: ${slug}\n`);
  await click(back);
}

/**
 * The editor mounted, with its text seeded by its own effect and validated by the server: the
 * validation is the one request not in the cache.
 */
async function mountSection(options: Parameters<typeof sectionTree>[0]) {
  stubEditorApi();
  const view = await mount(sectionTree(options));
  // Past the validation's debounce, then the answer.
  await settle(700);
  await settle();
  return view;
}

/** The commit button, and the words its `aria-describedby` points at, if any. */
function commitButton(): { button: HTMLElement | null; reason: string | null } {
  const unchanged = byText(document.body, "button", "No changes to commit");
  // With nothing to commit the button says so itself, rather than a reason beside it.
  if (unchanged) return { button: unchanged, reason: "No changes to commit" };
  const button = byText(document.body, "button", "Commit and sync");
  const id = button?.getAttribute("aria-describedby");
  return { button, reason: id ? (document.getElementById(id)?.textContent ?? null) : null };
}

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
  // A delete that drops unsaved text raises a flag with Undo; one left over would be the first
  // "Undo" the next test finds.
  clearFlags();
});

describe("ProcessEditorBody", () => {
  it("opens on process.yaml, with the explorer beside it rather than behind a button", () => {
    // Both halves on the first paint: the folder is the subject of the section, and the file it
    // opens on is the one the section is about. Nothing waits for a "review" click any more.
    const html = renderSection({ stale: [] });
    expect(html).toContain("<textarea"); // process.yaml, in the right pane
    // The explorer, open on its top level: the repo's own folders, each closed.
    expect(html).toContain('aria-label="Expand agents"');
    expect(html).not.toContain("Review what the PR will create");
  });

  it("carries a manifest's warning into the explorer, where the agents row used to be", async () => {
    // Marked on the manifest's own row on the first paint…
    // The explorer opens on its top level, so the manifest's folder is closed on the first paint.
    expect(renderSection({ stale: [] })).toContain('aria-label="Expand agents"');
    // …and what it says is the listing's own words, one press away.
    stubEditorApi();
    const view = await mount(sectionTree({ stale: [] }));
    await expandTree();
    await click(view.container.querySelector('[aria-label="Unusable: agent.yaml"]'));
    expect(document.querySelector('[role="dialog"]')?.textContent).toContain(
      "plan-agent: empty document",
    );
    await view.unmount();
  });

  it("offers to discard edits only once there is something to discard, and never calls it a draft", () => {
    const withEdits = renderSection({ stale: [INSTRUCTIONS] });
    expect(withEdits).toContain("Discard edits");
    expect(withEdits).not.toContain("Uncommitted draft");
    expect(withEdits).not.toContain("Discard draft");

    expect(renderSection({ stale: [] })).not.toContain("Discard edits");
  });

  it("no longer buries offered-variant reasons in the explorer", () => {
    expect(renderSection({ stale: [] })).not.toContain("not offered");
  });

  it("holds the commit shut while a stale file is unconfirmed, and says why beside it", async () => {
    // Held, not disabled: the button keeps its tab stop and the reason is on the page, linked to
    // it — a disabled button's reason could only ever live in a tooltip nobody can reach.
    const view = await mountSection({ stale: [INSTRUCTIONS] });
    const { button, reason } = commitButton();
    expect(button?.getAttribute("aria-disabled")).toBe("true");
    expect(button?.hasAttribute("disabled")).toBe(false);
    expect(reason).toBe("1 file changed since you edited it. Review it before you commit.");
    await view.unmount();
  });

  it("allows the commit when nothing is stale", async () => {
    const view = await mountSection({ stale: [] });
    const { button, reason } = commitButton();
    expect(button?.hasAttribute("aria-disabled")).toBe(false);
    expect(reason).toBeNull();
    await view.unmount();
  });

  it("keeps the caret where the operator typed in a proposed file", async () => {
    const view = await mountSection({ stale: [] });
    await click(await treeFile("instructions.md"));
    const area = document.querySelector<HTMLTextAreaElement>(
      `textarea[aria-label="${INSTRUCTIONS}"]`,
    );
    if (!area) throw new Error("the proposed file did not open");
    typeAt(area, 3, "X");
    expect(area.value).toBe("newX template body");
    expect(area.selectionStart).toBe(4);
    await view.unmount();
  });
});

describe("ProcessEditorBody, while the pinned config is being read", () => {
  it("draws a loading state, not an empty editor that asks for a template", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => {})),
    );
    const view = await mount(sectionTree({ stale: [], pinnedUnread: true }));
    await settle();
    expect(document.body.textContent).toContain("Loading the config");
    expect(textareaFor("process.yaml")).toBeNull();
    expect(
      document.querySelector('[role="status"][aria-label="Loading the files"]'),
    ).not.toBeNull();
    expect(commitButton().reason).toBe("Loading the config…");
    await view.unmount();
  });
});

describe("ProcessEditorBody runtime", () => {
  it("offers no Runtime select: the text's own runtime line is where it is set", () => {
    const html = renderSection({ stale: [], runtime: "claude-api" });
    expect(html).not.toContain("<select");
    expect(html).not.toContain("Runtime: ");
  });
});

describe("ProcessEditorBody templates", () => {
  it("replaces the text with the template, naming the slug the lane runs on", async () => {
    // The template names no Runtime; the lane keeps the one it already names (decision 36).
    const view = await mountSection({ stale: [], runtime: "codex-prod" });
    const item = await openTemplateItem();
    expect(item?.textContent).toContain(SIMPLE_TEMPLATE.description);
    await click(item);
    expect(textareaFor("process.yaml")?.value).toContain(
      "\n# runtime: claude-api\nruntime: codex-prod\n",
    );
    await view.unmount();
    clearFlags();
  });

  it("keeps the edited lane's name, so the commit replaces that lane", async () => {
    // With the template's own name the commit would add a `simple` lane beside this one.
    const view = await mountSection({ stale: [], runtime: null });
    await click(await openTemplateItem());
    expect(textareaFor("process.yaml")?.value).toBe(
      SIMPLE_LANE.yaml.replace("name: simple", `name: ${PROCESS_KEY}`),
    );
    await view.unmount();
    clearFlags();
  });

  it("gives a lane not yet written the template's own name", async () => {
    const view = await mountSection({ stale: [], runtime: null, laneless: true });
    await click(await openTemplateItem());
    expect(textareaFor("process.yaml")?.value).toBe(SIMPLE_LANE.yaml);
    await view.unmount();
    clearFlags();
  });

  it("offers each template once, and a template with two processes gives its first", async () => {
    const pair = {
      ...SIMPLE_TEMPLATE,
      lanes: [
        { ...SIMPLE_LANE, key: "feature", yaml: SIMPLE_LANE.yaml.replace("simple", "feature") },
        { ...SIMPLE_LANE, key: "hotfix", yaml: SIMPLE_LANE.yaml.replace("simple", "hotfix") },
      ],
    };
    const second = { ...SIMPLE_TEMPLATE, key: "superpowers", title: "Superpowers template" };
    const view = await mountSection({
      stale: [],
      runtime: null,
      laneless: true,
      templates: [pair, second],
    });
    await click(byText(document.body, "button", "Import"));
    const items = [...document.querySelectorAll('[role="menuitem"]')];
    expect(items.map((item) => item.textContent?.replace(pair.description, ""))).toEqual([
      "Simple lane",
      "Superpowers template",
    ]);
    await click(items[0]);
    expect(textareaFor("process.yaml")?.value).toBe(pair.lanes[0]?.yaml);
    await view.unmount();
    clearFlags();
  });

  it("shows no file menu: Import a file, Export and Copy are not offered", async () => {
    const view = await mountSection({ stale: [], runtime: null });
    expect(document.querySelector('[aria-label="Editor actions"]')).toBeNull();
    expect(document.querySelector('input[type="file"]')).toBeNull();
    await view.unmount();
  });
});

describe("ProcessEditorBody, when things go back or go wrong", () => {
  it("puts the operator's ticks back with the text when Undo reverses a discard", async () => {
    // The text returning is part of what Undo restores, not a new edit: the rule that an edited
    // process.yaml drops the ticks must not take back the ticks Undo is putting back.
    stubEditorApi();
    const edited = `${RAW_TEXT}# tuned\n`;
    const view = await mount(
      <>
        {sectionTree({ stale: [INSTRUCTIONS], edited })}
        <FlagGroup />
      </>,
    );
    await settle(700);
    await settle();
    await type(document.querySelector('textarea[aria-label="process.yaml"]'), edited);
    await settle(700);
    await settle();
    await expandTree();
    const box = () =>
      byText(document.body, "label", "Include instructions.md")?.querySelector("input") ?? null;
    const chosen = !box()?.checked;
    await click(box());
    expect(box()?.checked).toBe(chosen);
    await click(byText(document.body, "button", "Discard edits"));
    await click(byText(document.body, "button", "Undo"));
    await settle();
    expect(box()?.checked).toBe(chosen);
    await view.unmount();
    clearFlags();
  });

  it("says the check failed when the server could not check the config", async () => {
    // Not "Checking the config…" for ever: nothing is running once the request has failed.
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ detail: "the validator is down" }), {
            status: 503,
            headers: { "Content-Type": "application/json" },
          }),
      ),
    );
    const view = await mount(sectionTree({ stale: [] }));
    await settle(700);
    await settle();
    expect(commitButton().reason).toBe(
      "Factorial couldn't check the config. Edit it to check again.",
    );
    await view.unmount();
  });

  it("re-arms after a commit while a ticked file is still to be written", async () => {
    const processFile = `.factorial/processes/process-${PROCESS_KEY}.yaml`;
    const commits = stubEditorApi({
      // What the repo holds once instructions.md is committed: system-prompt.md is still new.
      preview: async () =>
        jsonResponse({
          files: [
            { ...INSTRUCTIONS_PROPOSAL, present_in_repo: true },
            proposal(SECOND, "second template"),
          ],
          components: [],
        }),
      commit: () =>
        jsonResponse(
          {
            sha: "e".repeat(40),
            url: "https://github.com/northwind/harbor-invoices/commit/eee",
            branch: "factorial",
            committed: true,
            components: [],
            written: [processFile, INSTRUCTIONS],
            sync: null,
            sync_error: null,
          } satisfies ConfigCommitResult,
          201,
        ),
    });
    const view = await mount(
      sectionTree({
        stale: [],
        proposals: [INSTRUCTIONS_PROPOSAL, proposal(SECOND, "second template")],
      }),
    );
    await settle(700);
    await settle();
    await expandTree();
    await click(byText(document.body, "label", "Include system-prompt.md")?.querySelector("input"));
    await click(commitButton().button);
    await settle();
    await settle();

    expect(commits[0]?.files.map((file) => file.path)).toEqual([INSTRUCTIONS]);
    expect(commitButton().reason).toBeNull();
    await view.unmount();
  });
});

describe("ProcessEditorBody, while the preview reloads", () => {
  async function editBothThenPickARuntime() {
    const view = await mount(
      sectionTree({
        stale: [],
        proposals: [INSTRUCTIONS_PROPOSAL, proposal(SECOND, "second template")],
      }),
    );
    await settle(700);
    await settle();
    await click(await treeFile("system-prompt.md"));
    await type(textareaFor(SECOND), "second edit\n");
    await click(await treeFile("instructions.md"));
    await type(textareaFor(INSTRUCTIONS), "first edit\n");
    // A runtime line rewrites process.yaml while instructions.md stays open in its own tab.
    await typeRuntime("codex-prod");
    await settle(700);
    await settle();
    return view;
  }

  it("keeps every held edit, in the tree and the draft, until the new preview lands", async () => {
    stubEditorApi({ preview: () => new Promise<Response>(() => {}) });
    const view = await editBothThenPickARuntime();

    expect(textareaFor(INSTRUCTIONS)?.value).toBe("first edit\n");
    expect(await treeFile("system-prompt.md")).not.toBeNull();
    expect(commitButton().reason).toBe("Checking which files the commit would write…");

    await type(textareaFor(INSTRUCTIONS), "first edit, then more\n");
    expect(Object.keys(readBundleDraft(PROJECT_ID, PROCESS_KEY) ?? {}).sort()).toEqual(
      [INSTRUCTIONS, SECOND].sort(),
    );
    await view.unmount();
  });

  it("keeps held edits when the preview request fails", async () => {
    stubEditorApi({ preview: async () => jsonResponse({ detail: "boom" }, 500) });
    const view = await editBothThenPickARuntime();

    expect(textareaFor(INSTRUCTIONS)?.value).toBe("first edit\n");
    await type(textareaFor(INSTRUCTIONS), "first edit, then more\n");
    expect(Object.keys(readBundleDraft(PROJECT_ID, PROCESS_KEY) ?? {}).sort()).toEqual(
      [INSTRUCTIONS, SECOND].sort(),
    );
    await view.unmount();
  });
});

describe("ProcessEditorBody's file tabs", () => {
  const tabs = () =>
    [...document.querySelectorAll('[aria-label="Open files"] [role="tab"]')].map(
      (tab) => tab.textContent,
    );

  async function editTwoFiles() {
    stubEditorApi();
    const view = await mount(
      sectionTree({
        stale: [],
        proposals: [INSTRUCTIONS_PROPOSAL, proposal(SECOND, "second template")],
      }),
    );
    await settle(700);
    await settle();
    await click(await treeFile("instructions.md"));
    await type(textareaFor(INSTRUCTIONS), "first edit\n");
    await click(await treeFile("system-prompt.md"));
    await type(textareaFor(SECOND), "second edit\n");
    return view;
  }

  it("opens each file in a tab of its own, and switching back finds the edit", async () => {
    const view = await editTwoFiles();
    expect(tabs()).toHaveLength(3);
    expect(tabs()[0]).toMatch(/^process-standard\.yaml/);
    expect(document.querySelectorAll('[aria-label="Edited"]')).toHaveLength(2);
    await click(fileTab("instructions.md"));
    expect(textareaFor(INSTRUCTIONS)?.value).toBe("first edit\n");
    expect(textareaFor(SECOND)).toBeNull();
    await view.unmount();
  });

  it("closing a tab puts the file away and keeps its edit for the commit", async () => {
    const view = await editTwoFiles();
    await click(document.querySelector('[aria-label="Close system-prompt.md"]'));
    expect(tabs()).toHaveLength(2);
    // The neighbour takes the closed tab's place.
    expect(textareaFor(INSTRUCTIONS)?.value).toBe("first edit\n");
    expect(readBundleDraft(PROJECT_ID, PROCESS_KEY)?.[SECOND]?.body).toBe("second edit\n");
    expect(document.body.textContent).toContain("2 files edited");
    await view.unmount();
  });

  it("says so when every tab is closed", async () => {
    const view = await editTwoFiles();
    for (const name of ["process-standard.yaml", "instructions.md", "system-prompt.md"]) {
      await click(document.querySelector(`[aria-label="Close ${name}"]`));
    }
    expect(tabs()).toHaveLength(0);
    expect(document.body.textContent).toContain("No file is open");
    await view.unmount();
  });

  it("numbers the lines beside the text", () => {
    const html = renderSection({ stale: [] });
    expect(html).toMatch(/aria-hidden="true"[^>]*>(?:<div>\d+<\/div>)+/);
  });
});

describe("ProcessEditorBody, editing files the repo already has", () => {
  const PROMPT = ".factorial/agents/plan-agent/prompt-notes.md";
  const BLOB = "b".repeat(40);
  const OTHER = ".factorial/agents/plan-agent/other-notes.md";
  const PROCESS_FILE = `.factorial/processes/process-${PROCESS_KEY}.yaml`;
  const inRepo = (overrides: Partial<ConfigFile> = {}): ConfigFile => ({
    path: PROMPT,
    kind: "prompt",
    drift: "same",
    warning: null,
    sha: BLOB,
    ...overrides,
  });
  const committedResult = (written: string[]): ConfigCommitResult => ({
    sha: "e".repeat(40),
    url: "https://github.com/northwind/harbor-invoices/commit/eee",
    branch: "factorial",
    committed: true,
    components: [],
    written,
    sync: null,
    sync_error: null,
  });

  async function mountWith(options: Partial<Parameters<typeof sectionTree>[0]>) {
    const view = await mount(
      sectionTree({
        stale: [],
        repoFiles: [inRepo()],
        fileBodies: { [PROMPT]: "my prompt\n" },
        ...options,
      }),
    );
    await settle(700);
    await settle();
    return view;
  }

  /** The explorer's form for a new file or folder, opened, with `value` typed and submitted. */
  async function createEntry(kind: "file" | "folder", value: string) {
    const label = () => byText(document.body, "label", `New ${kind} in .factorial/`);
    // The button toggles the form, so a form a refusal left open is typed into again.
    if (!label()) await click(document.querySelector(`[aria-label="New ${kind}"]`));
    const id = label()?.getAttribute("for");
    await type(id ? document.getElementById(id) : null, value);
    await click(byText(document.body, "button", kind === "file" ? "Create file" : "Create folder"));
  }

  it("creates a new file, opens it in a tab, and commits it as created", async () => {
    const path = ".factorial/notes/runbook.md";
    const commits = stubEditorApi({
      commit: () => jsonResponse(committedResult([PROCESS_FILE, path]), 201),
    });
    const view = await mountWith({ proposals: [] });
    await createEntry("file", "notes/runbook.md");
    expect(fileTab("runbook.md")?.getAttribute("aria-selected")).toBe("true");
    expect(document.body.textContent).toContain("New file. The next commit writes it.");
    await type(textareaFor(path), "# runbook\n");
    expect(commitButton().reason).toBeNull();

    await click(commitButton().button);
    await settle();
    expect((commits[0] as { created?: unknown }).created).toEqual([{ path, body: "# runbook\n" }]);
    expect(readCreatedFiles(PROJECT_ID, PROCESS_KEY)).toEqual({});
    await view.unmount();
  });

  it("refuses a name the server would refuse, and creates nothing", async () => {
    stubEditorApi();
    const view = await mountWith({ proposals: [] });
    await createEntry("file", "../escape.md");
    expect(document.body.textContent).toContain("starts with a letter or digit");
    await createEntry("file", "agents/plan-agent/prompt-notes.md/x.md");
    expect(document.body.textContent).toContain("Part of that path is a file");
    expect(readCreatedFiles(PROJECT_ID, PROCESS_KEY)).toEqual({});
    await view.unmount();
  });

  it("draws a new folder, open, before it holds a file", async () => {
    stubEditorApi();
    const view = await mountWith({ proposals: [] });
    await createEntry("folder", "notes");
    expect(document.querySelector('[aria-label="Collapse notes"]')).not.toBeNull();
    expect(readCreatedFiles(PROJECT_ID, PROCESS_KEY)).toEqual({});
    await view.unmount();
  });

  it("deleting a new file drops it and closes its tab", async () => {
    stubEditorApi();
    const view = await mountWith({ proposals: [] });
    await createEntry("file", "notes/runbook.md");
    await click(byText(document.body, "button", "Delete this new file"));
    expect(fileTab("runbook.md")).toBeNull();
    expect(readCreatedFiles(PROJECT_ID, PROCESS_KEY)).toEqual({});
    await view.unmount();
  });

  const menuItem = (label: string) =>
    [...document.querySelectorAll('[role="menuitem"]')].find((element) =>
      element.textContent?.startsWith(label),
    ) ?? null;
  const OTHER_PROCESS = ".factorial/processes/process-other.yaml";
  const deletedIn = (commit: unknown) => (commit as { deleted?: unknown }).deleted;

  it("stages a delete, sends it with the blob it started from, and keeps nothing staged after the commit", async () => {
    const commits = stubEditorApi({
      commit: () => jsonResponse({ ...committedResult([PROCESS_FILE]), deleted: [PROMPT] }, 201),
    });
    const view = await mountWith({ proposals: [] });
    await expandTree();
    await click(document.querySelector('[aria-label="Delete prompt-notes.md"]'));
    expect(readDeletedFiles(PROJECT_ID, PROCESS_KEY)).toEqual({ [PROMPT]: BLOB });
    expect(document.body.textContent).toContain("The next commit deletes 1 file");
    expect(document.querySelector('[aria-label="Restore prompt-notes.md"]')).not.toBeNull();
    expect(commitButton().reason).toBeNull();

    await click(commitButton().button);
    await settle();
    expect(deletedIn(commits[0])).toEqual([{ path: PROMPT, base_sha: BLOB }]);
    expect(readDeletedFiles(PROJECT_ID, PROCESS_KEY)).toEqual({});
    await view.unmount();
  });

  it("puts a staged delete back from its row", async () => {
    stubEditorApi();
    const view = await mountWith({ proposals: [] });
    await expandTree();
    await click(document.querySelector('[aria-label="Delete prompt-notes.md"]'));
    await click(document.querySelector('[aria-label="Restore prompt-notes.md"]'));
    expect(readDeletedFiles(PROJECT_ID, PROCESS_KEY)).toEqual({});
    expect(commitButton().reason).toBe("No changes to commit");
    await view.unmount();
  });

  it("drops a held edit when its file is deleted, and closes its tab", async () => {
    stubEditorApi();
    writeRepoEdits(PROJECT_ID, PROCESS_KEY, { [PROMPT]: { body: "mine\n", base: BLOB } });
    const view = await mountWith({ proposals: [] });
    await click(await treeFile("prompt-notes.md"));
    expect(fileTab("prompt-notes.md")).not.toBeNull();
    await click(document.querySelector('[aria-label="Delete prompt-notes.md"]'));
    expect(readRepoEdits(PROJECT_ID, PROCESS_KEY)).toEqual({});
    expect(fileTab("prompt-notes.md")).toBeNull();
    await view.unmount();
  });

  it("deletes every file in a folder from the folder's menu, and restores them from it", async () => {
    stubEditorApi();
    const view = await mountWith({
      proposals: [],
      repoFiles: [inRepo(), inRepo({ path: OTHER })],
    });
    await expandTree();
    await click(document.querySelector('[aria-label="Actions for plan-agent"]'));
    await click(menuItem("Delete folder"));
    expect(Object.keys(readDeletedFiles(PROJECT_ID, PROCESS_KEY)).sort()).toEqual(
      [".factorial/agents/plan-agent/agent.yaml", OTHER, PROMPT].sort(),
    );
    expect(document.body.textContent).toContain("The next commit deletes 3 files");
    await click(document.querySelector('[aria-label="Actions for plan-agent"]'));
    await click(menuItem("Restore folder"));
    expect(readDeletedFiles(PROJECT_ID, PROCESS_KEY)).toEqual({});
    await view.unmount();
  });

  it("puts a staged delete back with Discard edits' Undo", async () => {
    stubEditorApi();
    const view = await mount(
      <>
        {sectionTree({
          stale: [],
          proposals: [],
          repoFiles: [inRepo()],
          fileBodies: { [PROMPT]: "my prompt\n" },
        })}
        <FlagGroup />
      </>,
    );
    await settle(700);
    await settle();
    await expandTree();
    await click(document.querySelector('[aria-label="Delete prompt-notes.md"]'));
    await click(byText(document.body, "button", "Discard edits"));
    expect(readDeletedFiles(PROJECT_ID, PROCESS_KEY)).toEqual({});
    await click(byText(document.body, "button", "Undo"));
    await settle();
    expect(readDeletedFiles(PROJECT_ID, PROCESS_KEY)).toEqual({ [PROMPT]: BLOB });
    await view.unmount();
    clearFlags();
  });

  it("asks before a commit that deletes a process file, naming the process", async () => {
    const commits = stubEditorApi({
      commit: () =>
        jsonResponse({ ...committedResult([PROCESS_FILE]), deleted: [OTHER_PROCESS] }, 201),
    });
    const view = await mountWith({
      proposals: [],
      repoFiles: [inRepo({ path: OTHER_PROCESS, kind: "process" })],
    });
    await expandTree();
    await click(document.querySelector('[aria-label="Delete process-other.yaml"]'));
    await click(commitButton().button);
    await settle();
    expect(commits).toHaveLength(0);
    expect(document.body.textContent).toContain("Delete the other process?");
    expect(document.body.textContent).toContain("Tickets already on it keep running.");

    await click(byText(document.body, "button", "Commit and delete"));
    await settle();
    expect(deletedIn(commits[0])).toEqual([{ path: OTHER_PROCESS, base_sha: BLOB }]);
    await view.unmount();
  });

  it("deletes the process on screen: says so on its tab and sends no files for it", async () => {
    const commits = stubEditorApi({
      preview: async () => jsonResponse({ files: [INSTRUCTIONS_PROPOSAL], components: [] }),
      commit: () => jsonResponse({ ...committedResult([]), deleted: [PROCESS_FILE] }, 201),
    });
    const view = await mountWith({
      repoFiles: [
        inRepo({ path: PROCESS_FILE, kind: "process" }),
        inRepo({ path: OTHER_PROCESS, kind: "process" }),
      ],
    });
    await expandTree();
    await click(document.querySelector(`[aria-label="Delete process-${PROCESS_KEY}.yaml"]`));
    expect(document.body.textContent).toContain("This process is deleted in the next commit");
    expect(textareaFor("process.yaml")?.readOnly).toBe(true);

    await click(commitButton().button);
    await settle();
    expect(document.body.textContent).toContain(`Delete the ${PROCESS_KEY} process?`);
    await click(byText(document.body, "button", "Commit and delete"));
    await settle();
    expect(commits[0]?.files).toEqual([]);
    expect(deletedIn(commits[0])).toEqual([{ path: PROCESS_FILE, base_sha: BLOB }]);
    await view.unmount();
  });

  it("holds the commit while a staged delete is stale", async () => {
    stubEditorApi();
    // Staged against an older blob than the listing now gives the file.
    writeDeletedFiles(PROJECT_ID, PROCESS_KEY, { [PROMPT]: "c".repeat(40) });
    const view = await mountWith({ proposals: [] });
    expect(commitButton().reason).toMatch(/1 file changed since you edited it/);
    await view.unmount();
  });

  const VALID_CHECK = {
    valid: true,
    problems: [],
    warnings: [],
    would_write: [PROCESS_FILE, ".factorial/notes/runbook.md"],
    would_delete: [PROMPT],
  };

  it("validates the whole draft without committing, and says what a commit would do", async () => {
    const checked: unknown[] = [];
    const commits = stubEditorApi({
      check: (draft) => {
        checked.push(draft);
        return jsonResponse(VALID_CHECK);
      },
      commit: () => jsonResponse(committedResult([PROCESS_FILE]), 201),
    });
    const view = await mountWith({ proposals: [] });
    await createEntry("file", "notes/runbook.md");
    await expandTree();
    await click(document.querySelector('[aria-label="Delete prompt-notes.md"]'));
    await click(byText(document.body, "button", "Validate"));
    await settle();
    expect(checked).toEqual([
      {
        content: RAW_TEXT,
        files: [],
        edits: [],
        created: [{ path: ".factorial/notes/runbook.md", body: "" }],
        deleted: [{ path: PROMPT, base_sha: BLOB }],
      },
    ]);
    expect(commits).toHaveLength(0);
    expect(document.body.textContent).toContain("The whole config is valid");
    expect(document.body.textContent).toContain("A commit would write 2 files and delete 1.");
    await view.unmount();
  });

  it("names each problem's file with a button that opens it", async () => {
    stubEditorApi({
      check: () =>
        jsonResponse({
          ...VALID_CHECK,
          valid: false,
          problems: [
            { path: OTHER, message: "The file is empty or holds only comments.", hint: null },
            { path: null, message: "The factorial branch moved.", hint: "Sync config first." },
          ],
        }),
    });
    const view = await mountWith({
      proposals: [],
      repoFiles: [inRepo(), inRepo({ path: OTHER })],
      fileBodies: { [PROMPT]: "my prompt\n", [OTHER]: "" },
    });
    await click(byText(document.body, "button", "Validate"));
    await settle();
    expect(document.body.textContent).toContain("The config has 2 problems");
    expect(document.body.textContent).toContain("The file is empty or holds only comments.");
    expect(document.body.textContent).toContain("Sync config first.");
    await click(byText(document.body, "button", OTHER));
    expect(fileTab("other-notes.md")?.getAttribute("aria-selected")).toBe("true");
    await view.unmount();
  });

  it("drops the verdict when the draft changes after it", async () => {
    stubEditorApi({ check: () => jsonResponse(VALID_CHECK) });
    const view = await mountWith({ proposals: [] });
    await click(byText(document.body, "button", "Validate"));
    await settle();
    expect(document.body.textContent).toContain("The whole config is valid");
    await expandTree();
    await click(document.querySelector('[aria-label="Delete prompt-notes.md"]'));
    // The mutation tells its observers on the next tick.
    await settle();
    expect(document.body.textContent).not.toContain("The whole config is valid");
    await view.unmount();
  });

  it("offers to open the file a refused commit names", async () => {
    stubEditorApi({
      commit: () =>
        jsonResponse(
          { detail: `${OTHER}: The file is empty or holds only comments.`, path: OTHER },
          422,
        ),
    });
    const view = await mountWith({
      proposals: [],
      repoFiles: [inRepo(), inRepo({ path: OTHER })],
      fileBodies: { [PROMPT]: "my prompt\n", [OTHER]: "" },
    });
    await expandTree();
    await click(document.querySelector('[aria-label="Delete prompt-notes.md"]'));
    await click(commitButton().button);
    await settle();
    expect(document.body.textContent).toContain("The file is empty or holds only comments.");
    await click(byText(document.body, "button", "Open other-notes.md"));
    expect(fileTab("other-notes.md")?.getAttribute("aria-selected")).toBe("true");
    await view.unmount();
  });

  const MANIFEST = ".factorial/agents/plan-agent/agent.yaml";
  const menuLabels = () =>
    [...document.querySelectorAll('[role="menuitem"]')].map((item) => item.textContent);
  const folderRow = (name: string) =>
    document.querySelector(`[aria-label="Collapse ${name}"]`)?.parentElement ?? null;
  async function rightClick(element: Element | null) {
    if (!element) throw new Error("nothing to right-click");
    await act(async () => {
      element.dispatchEvent(
        new MouseEvent("contextmenu", {
          bubbles: true,
          cancelable: true,
          clientX: 30,
          clientY: 50,
        }),
      );
    });
    await settle();
  }
  /** The explorer's prompt for a construct, with `name` typed and submitted. */
  async function nameConstruct(label: string, name: string, submit: string) {
    const id = byText(document.body, "label", label)?.getAttribute("for");
    await type(id ? document.getElementById(id) : null, name);
    await click(byText(document.body, "button", submit));
  }
  const createdIn = (commit: unknown) => (commit as { created?: SelectedFile[] }).created ?? [];

  it("right-clicking the config folder offers the four constructs, then a file and a folder", async () => {
    stubEditorApi();
    const view = await mountWith({ proposals: [] });
    await rightClick(folderRow(".factorial"));
    expect(document.querySelector('[role="menu"]')?.getAttribute("aria-label")).toBe(
      "Actions for .factorial",
    );
    // No Delete folder: the config folder itself is never one to delete.
    expect(menuLabels()).toEqual(["Agent", "MCP server", "Process", "Skill", "File", "Folder"]);
    const groups = [...document.querySelectorAll('[role="menu"] [role="group"]')];
    expect(groups.map((group) => group.getAttribute("aria-label"))).toEqual(["Add new"]);
    expect(
      [...document.querySelectorAll('[role="menuitem"] svg')].map((icon) =>
        icon.getAttribute("data-glyph"),
      ),
    ).toEqual(["agent", "plug", "lane", "file-markdown", "file-yaml", "folder"]);
    await view.unmount();
  });

  it("offers the same entries from the folder row's menu button, for a keyboard or a touch", async () => {
    stubEditorApi();
    const view = await mountWith({ proposals: [] });
    await expandTree();
    await click(document.querySelector('[aria-label="Actions for plan-agent"]'));
    expect(menuLabels()).toEqual(["MCP server", "Skill", "File", "Folder", "Delete folder"]);
    expect(
      [...document.querySelectorAll('[role="menu"] [role="group"]')].map((group) =>
        group.getAttribute("aria-label"),
      ),
    ).toEqual(["Add new", "This folder"]);
    await view.unmount();
  });

  it("offers the four constructs from the explorer's header", async () => {
    stubEditorApi();
    const view = await mountWith({ proposals: [] });
    await click(
      document.querySelector('[aria-label="Add an agent, MCP server, process or skill"]'),
    );
    expect(menuLabels()).toEqual(["Agent", "MCP server", "Process", "Skill"]);
    await view.unmount();
  });

  it("asks for the name in the explorer's prompt, then creates the agent's three files and opens its manifest", async () => {
    const commits = stubEditorApi({
      commit: () => jsonResponse(committedResult([PROCESS_FILE]), 201),
    });
    const view = await mountWith({ proposals: [] });
    await rightClick(folderRow(".factorial"));
    await click(menuItem("Agent"));
    await nameConstruct("New agent in .factorial/agents/", "review-agent", "Create agent");
    expect(fileTab("agent.yaml")?.getAttribute("aria-selected")).toBe("true");
    expect(textareaFor(".factorial/agents/review-agent/agent.yaml")?.value).toBe(
      "kind: Agent\nname: review-agent\n",
    );

    await click(commitButton().button);
    await settle();
    expect(createdIn(commits[0])).toEqual([
      {
        path: ".factorial/agents/review-agent/agent.yaml",
        body: "kind: Agent\nname: review-agent\n",
      },
      {
        path: ".factorial/agents/review-agent/instructions.md",
        body: "Instructions for `review-agent`.\n",
      },
      {
        path: ".factorial/agents/review-agent/system-prompt.md",
        body: "You are `review-agent`.\n",
      },
    ]);
    await view.unmount();
  });

  it("asks for the name in a prompt that floats where the menu was opened, and puts it away on a press elsewhere", async () => {
    stubEditorApi();
    const view = await mountWith({ proposals: [] });
    await rightClick(folderRow(".factorial"));
    await click(menuItem("Agent"));
    const prompt = () =>
      document.querySelector<HTMLElement>('[role="dialog"][aria-label="New agent"]');
    // Where the right-click was (30, 50), by the popup's own gap.
    expect(prompt()?.style.left).toBe("30px");
    expect(prompt()?.style.top).toBe("56px");
    expect(
      prompt()?.contains(byText(document.body, "label", "New agent in .factorial/agents/")),
    ).toBe(true);
    expect(prompt()?.contains(document.activeElement)).toBe(true);
    await pointerDown(document.body);
    expect(prompt()).toBeNull();
    expect(readCreatedFiles(PROJECT_ID, PROCESS_KEY)).toEqual({});
    await view.unmount();
  });

  it("floats the New file prompt too, from the header's button", async () => {
    stubEditorApi();
    const view = await mountWith({ proposals: [] });
    await click(document.querySelector('[aria-label="New file"]'));
    const prompt = document.querySelector('[role="dialog"][aria-label="New file"]');
    expect(prompt?.contains(byText(document.body, "label", "New file in .factorial/"))).toBe(true);
    await view.unmount();
  });

  it("has a border to drag between the file panel and the editor", async () => {
    stubEditorApi();
    const view = await mountWith({ proposals: [] });
    const border = document.querySelector('[role="separator"]');
    expect(border?.getAttribute("aria-label")).toBe("Resize the file panel");
    await view.unmount();
  });

  it("refuses a construct's name in the prompt, before any file exists", async () => {
    stubEditorApi();
    const view = await mountWith({ proposals: [] });
    await rightClick(folderRow(".factorial"));
    await click(menuItem("Agent"));
    await nameConstruct("New agent in .factorial/agents/", "plan-agent", "Create agent");
    expect(document.body.textContent).toContain("An agent with that name is already here.");
    expect(readCreatedFiles(PROJECT_ID, PROCESS_KEY)).toEqual({});
    await view.unmount();
  });

  it("makes an agent's own skill inside that agent's folder", async () => {
    stubEditorApi();
    const view = await mountWith({ proposals: [] });
    await expandTree();
    await rightClick(folderRow("plan-agent"));
    await click(menuItem("Skill"));
    await nameConstruct(
      "New skill for plan-agent in .factorial/agents/plan-agent/skills/",
      "notes",
      "Create skill",
    );
    expect(readCreatedFiles(PROJECT_ID, PROCESS_KEY)).toEqual({
      ".factorial/agents/plan-agent/skills/notes/SKILL.md": "---\nname: notes\n---\n",
    });
    await view.unmount();
  });

  it("gives a new process the runtime of the process on screen", async () => {
    stubEditorApi();
    const view = await mountWith({ proposals: [], runtime: "claude-api" });
    await rightClick(folderRow(".factorial"));
    await click(menuItem("Process"));
    await nameConstruct("New process in .factorial/processes/", "fast", "Create process");
    expect(readCreatedFiles(PROJECT_ID, PROCESS_KEY)).toEqual({
      ".factorial/processes/process-fast.yaml":
        "kind: Process\nname: fast\n# runtime: your-runtime\nruntime: claude-api\n",
    });
    await view.unmount();
  });

  it("starts a plain new file in the folder that was right-clicked", async () => {
    stubEditorApi();
    const view = await mountWith({ proposals: [] });
    await expandTree();
    await rightClick(folderRow("plan-agent"));
    await click(menuItem("File"));
    const id = byText(document.body, "label", "New file in .factorial/")?.getAttribute("for");
    const field = id ? (document.getElementById(id) as HTMLInputElement | null) : null;
    expect(field?.value).toBe("agents/plan-agent/");
    await view.unmount();
  });

  it("offers to create an agent a step names when it has no files", async () => {
    stubEditorApi();
    const view = await mountWith({
      proposals: [],
      components: [
        { name: "my-agent", kind: "agent", variants: [], present_in_repo: false, referenced: true },
      ],
    });
    expect(document.body.textContent).toContain("my-agent has no files yet");
    await click(byText(document.body, "button", "Create my-agent"));
    expect(Object.keys(readCreatedFiles(PROJECT_ID, PROCESS_KEY))).toEqual([
      ".factorial/agents/my-agent/agent.yaml",
      ".factorial/agents/my-agent/instructions.md",
      ".factorial/agents/my-agent/system-prompt.md",
    ]);
    expect(document.body.textContent).not.toContain("my-agent has no files yet");
    await view.unmount();
  });

  it("fills an empty manifest on the branch from the shell and adds the prompts it lacks", async () => {
    stubEditorApi();
    const view = await mountWith({
      proposals: [],
      fileBodies: { [PROMPT]: "my prompt\n", [MANIFEST]: "# SCAFFOLDED, NOT FINISHED.\n" },
    });
    await click(await treeFile("agent.yaml"));
    await click(byText(document.body, "button", "Fill in from the shell"));
    expect(readRepoEdits(PROJECT_ID, PROCESS_KEY)).toEqual({
      [MANIFEST]: { body: "kind: Agent\nname: plan-agent\n", base: "a".repeat(40) },
    });
    expect(readCreatedFiles(PROJECT_ID, PROCESS_KEY)).toEqual({
      ".factorial/agents/plan-agent/instructions.md": "Instructions for `plan-agent`.\n",
      ".factorial/agents/plan-agent/system-prompt.md": "You are `plan-agent`.\n",
    });
    await view.unmount();
  });

  const withFlags = async (options: Partial<Parameters<typeof sectionTree>[0]> = {}) => {
    const view = await mount(
      <>
        {sectionTree({
          stale: [],
          proposals: [],
          repoFiles: [inRepo()],
          fileBodies: { [PROMPT]: "my prompt\n" },
          ...options,
        })}
        <FlagGroup />
      </>,
    );
    await settle(700);
    await settle();
    return view;
  };

  it("does not call an assistant's draft out of date after its own commit", async () => {
    const drafted = `${RAW_TEXT}# reviewed twice\n`;
    const commits = stubEditorApi({
      commit: () => jsonResponse(committedResult([PROCESS_FILE]), 201),
    });
    const view = await mountWith({
      proposals: [],
      edited: drafted,
      initialDraft: {
        project_id: PROJECT_ID,
        process: PROCESS_KEY,
        content: drafted,
        files: [],
        edits: [],
        base_sha: CURRENT_SHA,
        would_write: [],
      },
    });
    expect(document.body.textContent).not.toContain("written against another commit");
    await click(commitButton().button);
    await settle();
    expect(commits).toHaveLength(1);
    // The commit moved what the editor is based on; the draft is no older than it was.
    expect(document.body.textContent).not.toContain("written against another commit");
    await view.unmount();
  });

  it("closes when a commit deletes the file of the process it opened on, whatever the text is named now", async () => {
    const onBack = vi.fn();
    const commits = stubEditorApi({
      // The text was renamed, so the commit writes process-renamed.yaml and the old file is a
      // row like any other.
      parsed: { ...CONFIG, name: "renamed" },
      commit: () => jsonResponse({ ...committedResult([]), deleted: [PROCESS_FILE] }, 201),
    });
    const view = await mountWith({
      proposals: [],
      onBack,
      repoFiles: [inRepo({ path: PROCESS_FILE, kind: "process" })],
    });
    await expandTree();
    await click(document.querySelector(`[aria-label="Delete process-${PROCESS_KEY}.yaml"]`));
    await click(commitButton().button);
    await settle();
    await click(byText(document.body, "button", "Commit and delete"));
    await settle();
    expect(commits).toHaveLength(1);
    expect(onBack).toHaveBeenCalledTimes(1);
    await view.unmount();
  });

  it("drops a staged delete whose file has left the branch, and does not hold the commit", async () => {
    stubEditorApi();
    writeDeletedFiles(PROJECT_ID, PROCESS_KEY, { ".factorial/notes/gone.md": BLOB });
    const view = await mountWith({ proposals: [] });
    await settle();
    expect(readDeletedFiles(PROJECT_ID, PROCESS_KEY)).toEqual({});
    expect(commitButton().reason).toBe("No changes to commit");
    await view.unmount();
  });

  it("opens a file staged for deletion read-only, with the way to restore it", async () => {
    stubEditorApi();
    const view = await mountWith({ proposals: [] });
    await expandTree();
    await click(document.querySelector('[aria-label="Delete prompt-notes.md"]'));
    await click(await treeFile("prompt-notes.md"));
    expect(document.body.textContent).toContain("Deleted in the next commit.");
    expect(textareaFor(PROMPT)?.readOnly).toBe(true);
    await click(byText(document.body, "button", "Restore the file"));
    expect(readDeletedFiles(PROJECT_ID, PROCESS_KEY)).toEqual({});
    expect(textareaFor(PROMPT)?.readOnly).toBe(false);
    await view.unmount();
  });

  it("re-stages a stale delete at the blob the branch holds now", async () => {
    stubEditorApi();
    writeDeletedFiles(PROJECT_ID, PROCESS_KEY, { [PROMPT]: "c".repeat(40) });
    const view = await mountWith({ proposals: [] });
    expect(commitButton().reason).toMatch(/1 file changed since you edited it/);
    await expandTree();
    await click(byText(document.body, "button", "Review"));
    expect(document.body.textContent).toContain(
      "This file changed on the factorial branch since you deleted it",
    );
    await click(byText(document.body, "button", "Delete it anyway"));
    expect(readDeletedFiles(PROJECT_ID, PROCESS_KEY)).toEqual({ [PROMPT]: BLOB });
    expect(commitButton().reason).toBeNull();
    await view.unmount();
  });

  it("puts back what a delete dropped: a new file's text", async () => {
    stubEditorApi();
    const view = await withFlags();
    await createEntry("file", "notes/runbook.md");
    await type(textareaFor(".factorial/notes/runbook.md"), "# runbook\n");
    await click(document.querySelector('[aria-label="Delete runbook.md"]'));
    expect(readCreatedFiles(PROJECT_ID, PROCESS_KEY)).toEqual({});
    await click(byText(document.body, "button", "Undo"));
    await settle();
    expect(readCreatedFiles(PROJECT_ID, PROCESS_KEY)).toEqual({
      ".factorial/notes/runbook.md": "# runbook\n",
    });
    await view.unmount();
    clearFlags();
  });

  it("puts back what a delete dropped: an edit held for the deleted file", async () => {
    stubEditorApi();
    writeRepoEdits(PROJECT_ID, PROCESS_KEY, { [PROMPT]: { body: "mine\n", base: BLOB } });
    const view = await withFlags();
    await expandTree();
    await click(document.querySelector('[aria-label="Delete prompt-notes.md"]'));
    expect(readRepoEdits(PROJECT_ID, PROCESS_KEY)).toEqual({});
    await click(byText(document.body, "button", "Undo"));
    await settle();
    expect(readRepoEdits(PROJECT_ID, PROCESS_KEY)).toEqual({
      [PROMPT]: { body: "mine\n", base: BLOB },
    });
    expect(readDeletedFiles(PROJECT_ID, PROCESS_KEY)).toEqual({});
    await view.unmount();
    clearFlags();
  });

  it("sends a repo file, a new file and the process text in one commit", async () => {
    const commits = stubEditorApi({
      preview: async () => jsonResponse({ files: [INSTRUCTIONS_PROPOSAL], components: [] }),
      commit: () => jsonResponse(committedResult([PROCESS_FILE, INSTRUCTIONS, PROMPT]), 201),
    });
    const view = await mountWith({});
    await click(await treeFile("prompt-notes.md"));
    expect(textareaFor(PROMPT)?.value).toBe("my prompt\n");
    await type(textareaFor(PROMPT), "my tuned prompt\n");
    await click(await treeFile("instructions.md"));
    await type(textareaFor(INSTRUCTIONS), "my instructions\n");
    await typeRuntime("codex-prod");
    await settle(700);
    await settle();

    await click(commitButton().button);
    await settle();
    const sent = commits[0];
    expect(sent?.content).toContain("codex-prod");
    expect(sent?.files).toEqual([{ path: INSTRUCTIONS, body: "my instructions\n" }]);
    expect(sent?.edits).toEqual([{ path: PROMPT, body: "my tuned prompt\n", base_sha: BLOB }]);
    expect(readRepoEdits(PROJECT_ID, PROCESS_KEY)).toEqual({});
    await view.unmount();
  });

  it("keeps an edit the commit did not write, and names it", async () => {
    stubEditorApi({ commit: () => jsonResponse(committedResult([PROCESS_FILE]), 201) });
    const view = await mountWith({ proposals: [] });
    await click(await treeFile("prompt-notes.md"));
    await type(textareaFor(PROMPT), "my tuned prompt\n");
    await click(commitButton().button);
    await settle();
    expect(Object.keys(readRepoEdits(PROJECT_ID, PROCESS_KEY))).toEqual([PROMPT]);
    expect(document.body.textContent).toContain("Some edits are missing from the commit");
    await view.unmount();
  });

  it("drops the sent edits when the branch already held them", async () => {
    stubEditorApi({
      commit: () => jsonResponse({ ...committedResult([]), committed: false }, 201),
    });
    const view = await mountWith({ proposals: [] });
    await click(await treeFile("prompt-notes.md"));
    await type(textareaFor(PROMPT), "my tuned prompt\n");
    await click(commitButton().button);
    await settle();
    expect(readRepoEdits(PROJECT_ID, PROCESS_KEY)).toEqual({});
    expect(document.body.textContent).toContain("Nothing to commit");
    await view.unmount();
  });

  it("drops an edit typed back to the repo's copy", async () => {
    stubEditorApi();
    const view = await mountWith({ proposals: [] });
    expect(commitButton().reason).toBe("No changes to commit");
    await click(await treeFile("prompt-notes.md"));
    await type(textareaFor(PROMPT), "changed\n");
    expect(commitButton().reason).toBeNull();
    await type(textareaFor(PROMPT), "my prompt\n");
    expect(readRepoEdits(PROJECT_ID, PROCESS_KEY)).toEqual({});
    expect(commitButton().reason).toBe("No changes to commit");
    await view.unmount();
  });

  it("keeps a file the branch changed since the pin read-only, and says why", async () => {
    stubEditorApi();
    const view = await mountWith({ repoFiles: [inRepo({ drift: "differs" })] });
    await click(await treeFile("prompt-notes.md"));
    expect(textareaFor(PROMPT)?.readOnly).toBe(true);
    expect(document.body.textContent).toContain(
      "Changed on the factorial branch since this version was synced. Sync config to edit it.",
    );
    await view.unmount();
  });

  it("flags a held edit whose base is no longer the listed blob until it is kept", async () => {
    stubEditorApi();
    writeRepoEdits(PROJECT_ID, PROCESS_KEY, {
      [PROMPT]: { body: "old edit\n", base: "0".repeat(40) },
    });
    const view = await mountWith({ proposals: [] });
    expect(commitButton().reason).toBe(
      "1 file changed since you edited it. Review it before you commit.",
    );
    await click(await treeFile("prompt-notes.md"));
    expect(textareaFor(PROMPT)?.value).toBe("old edit\n");
    await click(byText(document.body, "button", "Use my edit anyway"));
    expect(readRepoEdits(PROJECT_ID, PROCESS_KEY)[PROMPT]?.base).toBe(BLOB);
    expect(commitButton().reason).toBeNull();
    await view.unmount();
  });

  it("keeps edits made while the commit is in flight", async () => {
    let finish: (response: Response) => void = () => {};
    const commits = stubEditorApi({
      commit: () => new Promise<Response>((resolve) => (finish = resolve)),
    });
    const view = await mountWith({
      repoFiles: [inRepo(), inRepo({ path: OTHER })],
      fileBodies: { [PROMPT]: "my prompt\n", [OTHER]: "other\n" },
    });
    await click(await treeFile("prompt-notes.md"));
    await type(textareaFor(PROMPT), "sent body\n");
    await click(commitButton().button);
    await settle();
    expect(commits).toHaveLength(1);

    await type(textareaFor(PROMPT), "sent body, then more\n");
    await click(await treeFile("other-notes.md"));
    await type(textareaFor(OTHER), "typed in flight\n");
    finish(jsonResponse(committedResult([PROCESS_FILE, PROMPT]), 201));
    await settle();
    await settle();

    const held = readRepoEdits(PROJECT_ID, PROCESS_KEY);
    expect(held[PROMPT]?.body).toBe("sent body, then more\n");
    expect(held[OTHER]?.body).toBe("typed in flight\n");
    await view.unmount();
  });

  it("opens a path the catalog also proposes on the repo's copy, and edits that", async () => {
    stubEditorApi();
    const view = await mountWith({
      proposals: [{ ...INSTRUCTIONS_PROPOSAL, present_in_repo: true }],
      repoFiles: [inRepo({ path: INSTRUCTIONS })],
      fileBodies: { [INSTRUCTIONS]: "the repo's instructions\n" },
    });
    await click(await treeFile("instructions.md"));
    expect(textareaFor(INSTRUCTIONS)?.value).toBe("the repo's instructions\n");
    await type(textareaFor(INSTRUCTIONS), "tuned\n");
    expect(readRepoEdits(PROJECT_ID, PROCESS_KEY)[INSTRUCTIONS]).toEqual({
      body: "tuned\n",
      base: BLOB,
    });
    await view.unmount();
  });

  it("does not count a bundle draft an earlier build kept for a repo file as an edit", () => {
    // Such a draft is never sent, so it must not badge the file or offer to discard it.
    const html = renderSection({
      stale: [INSTRUCTIONS],
      proposals: [{ ...INSTRUCTIONS_PROPOSAL, present_in_repo: true }],
      repoFiles: [inRepo({ path: INSTRUCTIONS })],
    });
    expect(html).not.toContain(">Edited<");
    expect(html).not.toContain("Discard edits");
  });

  it("lists a new lane's own file as added, beside what the branch already holds", async () => {
    // Validation names the lane, which is where the path of the file it will write comes from.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url.endsWith("/process-versions/validate")
          ? jsonResponse({
              valid: true,
              errors: [],
              warnings: [],
              content: { ...CONFIG, name: "hotfix" },
            })
          : new Promise<Response>(() => {}),
      ),
    );
    const view = await mount(
      sectionTree({ stale: [], laneless: true, initialText: "kind: Process\nname: hotfix\n" }),
    );
    await settle(700);
    await settle();
    const lane = await treeFile("process-hotfix.yaml");
    expect(lane).not.toBeNull();
    expect(lane?.closest("li")?.innerHTML).toContain("text-success");
    // The lane that already runs is listed from its own pin, so the new file reads as an addition.
    expect(await treeFile("process.yaml")).not.toBeNull();
    await view.unmount();
  });

  it("shows a file the branch has, in a lane with no version, read-only and says why", async () => {
    stubEditorApi();
    const present = { ...INSTRUCTIONS_PROPOSAL, present_in_repo: true };
    const view = await mount(
      sectionTree({
        stale: [],
        laneless: true,
        lanes: false,
        proposals: [present],
        edited: "name: x\n",
      }),
    );
    await settle();
    await type(textareaFor("process.yaml"), "name: x\n");
    await settle(700);
    await settle();
    await click(await treeFile("instructions.md"));
    expect(textareaFor(INSTRUCTIONS)?.readOnly).toBe(true);
    expect(document.body.textContent).toContain(
      "Already on the factorial branch. Commit this process first, then edit it from its own copy.",
    );
    await view.unmount();
  });
});

describe("EditorMessages", () => {
  it("names a sent path the commit did not write", () => {
    const html = renderToString(
      <EditorMessages
        committed={{
          sha: "e".repeat(40),
          url: "u",
          branch: "factorial",
          committed: true,
          components: [],
          written: [".factorial/processes/process-standard.yaml"],
          sync: null,
          sync_error: null,
        }}
        sent={[{ path: ".factorial/agents/plan-agent/prompt-notes.md", body: "x" }]}
        requestError={null}
        verdict={undefined}
      />,
    );
    expect(html).toContain("Some edits are missing from the commit");
    expect(html).toContain("prompt-notes.md");
  });

  it("says after a synced commit that running tickets keep their version", () => {
    const committed = {
      sha: "e".repeat(40),
      url: "u",
      branch: "factorial",
      components: [],
      written: [],
      sync: null,
      sync_error: null,
    };
    const note =
      "New tickets run it. Tickets already running keep the version they were admitted on.";
    for (const made of [true, false]) {
      const html = renderToString(
        <EditorMessages
          committed={{ ...committed, committed: made }}
          sent={[]}
          requestError={null}
          verdict={undefined}
        />,
      );
      expect(html).toContain(note);
    }
  });

  it("calls a commit the branch already held nothing to commit, and names no gap", () => {
    const html = renderToString(
      <EditorMessages
        committed={{
          sha: "e".repeat(40),
          url: "u",
          branch: "factorial",
          committed: false,
          components: [],
          written: [],
          sync: null,
          sync_error: null,
        }}
        sent={[{ path: ".factorial/agents/plan-agent/prompt-notes.md", body: "x" }]}
        requestError={null}
        verdict={undefined}
      />,
    );
    expect(html).toContain("Nothing to commit. The factorial branch already holds this");
    expect(html).not.toContain("Some edits are missing from the commit");
    expect(html).not.toContain("Committed");
  });
});

describe("ProcessEditorBody, a lane on an E2B runtime", () => {
  const E2B = { ...runtimeRow("codex-api-on-e2b", "codex_api"), details: { isolation: "e2b" } };
  const options = { stale: [], runtime: "codex-api-on-e2b", runtimes: [...RUNTIMES, E2B] };

  it("warns, and offers Permit and commit beside Commit and sync, when the project has not permitted it", async () => {
    const view = await mountSection(options);
    expect(document.body.textContent).toContain(
      "codex-api-on-e2b isn't permitted for this project",
    );
    expect(byText(document.body, "button", "Permit and commit")).not.toBeNull();
    expect(commitButton().button).not.toBeNull();
    await view.unmount();
  });

  it("says nothing once the project permits it", async () => {
    const view = await mountSection({ ...options, allowlist: ["codex-api-on-e2b"] });
    expect(document.body.textContent).not.toContain("isn't permitted");
    expect(byText(document.body, "button", "Permit and commit")).toBeNull();
    await view.unmount();
  });
});

/** The gallery's two writes, mounted: what the seed is sent, and what follows it. */
describe("ProcessSection's gallery", () => {
  const HOST_RUNTIME = runtimeRow("claude-api", "claude_api");
  const E2B_RUNTIME = {
    ...runtimeRow("claude-on-e2b", "claude_api"),
    details: { isolation: "e2b" },
  };

  function mountSectionWith(
    qc: QueryClient,
    project: ProjectDetail = NO_LANES,
    branch: ReadinessItem = branchItem("warn", "missing"),
  ) {
    return mount(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ProcessSection project={project} branch={branch} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  /** A proposal offering one file the repo lacks and one it already has. */
  const PROPOSAL = {
    files: [
      { ...proposal(".factorial/agents/plan-agent/agent.yaml", "name: plan-agent\n") },
      {
        ...proposal(".factorial/skills/project-constitution/SKILL.md", "# shared\n"),
        present_in_repo: true,
      },
    ],
    components: [],
  };

  /** Every request recorded by path tail with its body; the seed answers `seedStatus`, the
   *  preview offers `PROPOSAL`, a commit lands, and anything else never does. */
  function stubSectionApi(seedStatus = 201, permitStatus = 200, commitStatus = 201) {
    const sent: Array<{ path: string; body: unknown }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const path = url.replace(/^.*\/api\/projects\/\d+/, "");
        sent.push({ path, body: init?.body ? JSON.parse(String(init.body)) : undefined });
        if (path === "/permits")
          return permitStatus === 200
            ? jsonResponse(LANES)
            : jsonResponse({ detail: "permits are locked" }, permitStatus);
        if (path === "/config-branch")
          return seedStatus === 201
            ? jsonResponse({ branch: "factorial", sha: "c".repeat(40) }, 201)
            : jsonResponse(
                { detail: "northwind/harbor-invoices already has a factorial branch" },
                seedStatus,
              );
        if (path === "/config-preview") return jsonResponse(PROPOSAL);
        if (path === "/config-commits" && commitStatus !== 201)
          return jsonResponse({ detail: "the factorial branch moved" }, commitStatus);
        if (path === "/config-commits")
          return jsonResponse(
            {
              sha: "d".repeat(40),
              url: "https://github.com/x",
              branch: "factorial",
              committed: true,
              components: [],
              written: [],
              sync: null,
              sync_error: null,
            },
            201,
          );
        return new Promise<Response>(() => {});
      }),
    );
    return sent;
  }

  function sectionClient(runtimes: Runtime[] = [HOST_RUNTIME]) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    holding(qc);
    qc.setQueryData(keys.processTemplates, { templates: [SIMPLE_TEMPLATE] });
    qc.setQueryData(keys.runtimes, runtimes);
    qc.setQueryData(keys.projectReadiness(PROJECT_ID), { readiness: [], warnings: [] });
    qc.setQueryData(keys.processVersion(PROJECT_ID, LATEST_ID), {
      project_id: PROJECT_ID,
      process_key: PROCESS_KEY,
      source_commit_sha: CURRENT_SHA,
      content: CONFIG,
    });
    return qc;
  }

  const LANES = { ...PROJECT, settings: { runtime_allowlist: [] } } as unknown as ProjectDetail;

  const FROM_MAIN = {
    branch: "main",
    sha: "9".repeat(40),
    config_dir: ".factorial",
    lanes: [{ key: PROCESS_KEY, content: CONFIG, agents: [] }],
    error: null,
  };

  async function importSimpleLane(label = "Import process template") {
    await click(document.querySelector("[data-gallery-card]"));
    await click(byText(document.body, "button", label));
    await settle();
  }
  const PERMIT_AND_IMPORT = "Permit claude-on-e2b and import";

  it("copies main's folder, syncs it, and leaves the gallery at once rather than offering Import again", async () => {
    const sent = stubSectionApi();
    const qc = sectionClient();
    qc.setQueryData(keys.defaultBranchConfig(PROJECT_ID), FROM_MAIN);
    const view = await mountSectionWith(qc);
    await click(document.querySelector("[data-gallery-card]"));
    await click(byText(document.body, "button", "Import main's config"));
    await settle();

    expect(sent.find((call) => call.path === "/config-branch")?.body).toEqual({
      from: "default_branch",
    });
    expect(sent.find((call) => call.path === "/process-versions/sync")?.body).toEqual({
      ref: null,
    });
    expect(sent.some((call) => call.path === "/config-commits")).toBe(false);
    expect(document.querySelectorAll("[data-gallery-card]")).toHaveLength(0);
    await view.unmount();
  });

  it("keeps the lanes already synced when a template creates the branch", async () => {
    const sent = stubSectionApi();
    const view = await mountSectionWith(sectionClient(), LANES);
    await click(byText(document.body, "button", "Create the branch"));
    await importSimpleLane();

    expect(sent.find((call) => call.path === "/config-branch")?.body).toEqual({
      from: "default_branch",
    });
    await view.unmount();
  });

  it("leaves the gallery Create the branch opened when the seed is refused, and says why", async () => {
    const sent = stubSectionApi(409);
    const qc = sectionClient();
    const view = await mountSectionWith(qc, LANES);
    await click(byText(document.body, "button", "Create the branch"));
    await importSimpleLane();

    expect(document.querySelectorAll("[data-gallery-card]")).toHaveLength(0);
    expect(document.body.textContent).toContain("already has a factorial branch");
    expect(qc.getQueryState(keys.projectReadiness(PROJECT_ID))?.isInvalidated).toBe(true);
    // Nothing is committed onto a branch the import did not create.
    expect(sent.some((call) => call.path === "/config-commits")).toBe(false);
    await view.unmount();
  });

  it("seeds an empty branch, then commits the template, while the branch row still says missing", async () => {
    const sent = stubSectionApi();
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    holding(qc);
    qc.setQueryData(keys.processTemplates, { templates: [SIMPLE_TEMPLATE] });
    qc.setQueryData(keys.runtimes, [HOST_RUNTIME]);
    const view = await mountSectionWith(qc);
    await importSimpleLane();

    expect(sent.map((call) => call.path).filter((path) => path.startsWith("/config"))).toEqual([
      "/config-branch",
      "/config-preview",
      "/config-commits",
    ]);
    expect(sent.find((call) => call.path === "/config-branch")?.body).toEqual({ from: "empty" });
    await view.unmount();
  });

  it("imports every lane of a template in turn, and says which landed when one fails", async () => {
    const commits: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (url.endsWith("/config-branch"))
          return jsonResponse({ branch: "factorial", sha: "c".repeat(40) }, 201);
        if (url.endsWith("/config-preview")) return jsonResponse(PROPOSAL);
        if (url.endsWith("/config-commits")) {
          const { content } = JSON.parse(String(init?.body)) as { content: string };
          commits.push(content);
          return commits.length === 1
            ? jsonResponse({ sha: "d".repeat(40), committed: true, written: [] }, 201)
            : jsonResponse({ detail: "the factorial branch moved" }, 409);
        }
        return new Promise<Response>(() => {});
      }),
    );
    const qc = sectionClient();
    const pair = {
      ...SIMPLE_TEMPLATE,
      lanes: [
        { ...SIMPLE_LANE, key: "feature", yaml: SIMPLE_LANE.yaml.replace("simple", "feature") },
        { ...SIMPLE_LANE, key: "hotfix", yaml: SIMPLE_LANE.yaml.replace("simple", "hotfix") },
      ],
    };
    qc.setQueryData(keys.processTemplates, { templates: [pair] });
    const view = await mountSectionWith(qc, LANES);
    await click(byText(document.body, "button", "Create the branch"));
    await importSimpleLane();

    expect(commits.map((content) => content.match(/\nname: (\S+)/)?.[1])).toEqual([
      "feature",
      "hotfix",
    ]);
    expect(document.body.textContent).toContain(
      "Imported feature; hotfix was not imported: the factorial branch moved",
    );
    await view.unmount();
  });

  it("writes the picked runtime into every lane it commits", async () => {
    const commits: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (url.endsWith("/config-branch"))
          return jsonResponse({ branch: "factorial", sha: "c".repeat(40) }, 201);
        if (url.endsWith("/config-preview")) return jsonResponse(PROPOSAL);
        if (url.endsWith("/config-commits")) {
          commits.push((JSON.parse(String(init?.body)) as { content: string }).content);
          return jsonResponse({ sha: "d".repeat(40), committed: true, written: [] }, 201);
        }
        return new Promise<Response>(() => {});
      }),
    );
    const qc = sectionClient([HOST_RUNTIME, E2B_RUNTIME]);
    const pair = {
      ...SIMPLE_TEMPLATE,
      lanes: [
        { ...SIMPLE_LANE, key: "feature", yaml: SIMPLE_LANE.yaml.replace("simple", "feature") },
        { ...SIMPLE_LANE, key: "hotfix", yaml: SIMPLE_LANE.yaml.replace("simple", "hotfix") },
      ],
    };
    qc.setQueryData(keys.processTemplates, { templates: [pair] });
    const view = await mountSectionWith(qc);
    await click(document.querySelector("[data-gallery-card]"));
    await choose(document.querySelector('select[aria-label="Runtime"]'), "claude-api");
    await click(byText(document.body, "button", "Import process template"));
    await settle();

    expect(commits).toHaveLength(2);
    for (const content of commits) {
      expect(content.match(/^runtime: .*$/gm)).toEqual(["runtime: claude-api"]);
    }
    await view.unmount();
  });

  it("permits an E2B runtime the project lacks once every lane is committed", async () => {
    const sent = stubSectionApi();
    const view = await mountSectionWith(sectionClient([E2B_RUNTIME]), NO_LANES);
    await importSimpleLane(PERMIT_AND_IMPORT);

    const paths = sent.map((call) => call.path);
    expect(sent.find((call) => call.path === "/permits")?.body).toEqual({
      runtime_allowlist: { add: ["claude-on-e2b"] },
    });
    expect(paths).toContain("/config-commits");
    expect(paths.indexOf("/permits")).toBeGreaterThan(paths.lastIndexOf("/config-commits"));
    await view.unmount();
  });

  it("grants no permit when a lane's commit fails", async () => {
    const sent = stubSectionApi(201, 200, 409);
    const view = await mountSectionWith(sectionClient([E2B_RUNTIME]), NO_LANES);
    await importSimpleLane(PERMIT_AND_IMPORT);

    expect(sent.some((call) => call.path === "/config-commits")).toBe(true);
    expect(sent.some((call) => call.path === "/permits")).toBe(false);
    expect(document.body.textContent).toContain("the factorial branch moved");
    await view.unmount();
  });

  it("asks for no permit for a host runtime, nor one the project already permits", async () => {
    const sent = stubSectionApi();
    const permitted = {
      ...NO_LANES,
      settings: { runtime_allowlist: ["claude-on-e2b"] },
    } as unknown as ProjectDetail;
    const view = await mountSectionWith(sectionClient([E2B_RUNTIME]), permitted);
    await importSimpleLane();
    await view.unmount();
    const host = await mountSectionWith(sectionClient([HOST_RUNTIME]), NO_LANES);
    await importSimpleLane();
    await host.unmount();

    expect(sent.some((call) => call.path === "/permits")).toBe(false);
    expect(sent.filter((call) => call.path === "/config-commits")).toHaveLength(2);
  });

  it("keeps the lanes, and says the runtime isn't permitted, when the permit is refused", async () => {
    const sent = stubSectionApi(201, 403);
    const view = await mountSectionWith(sectionClient([E2B_RUNTIME]), NO_LANES);
    await importSimpleLane(PERMIT_AND_IMPORT);

    expect(sent.some((call) => call.path === "/config-commits")).toBe(true);
    expect(document.body.textContent).toContain(
      "Imported, but claude-on-e2b isn't permitted for this project",
    );
    expect(document.body.textContent).toContain("permits are locked");
    expect(document.body.textContent).toContain(
      "Permit it under Runtime, in Cloud sandbox runtimes, before its processes can run.",
    );
    expect(byText(document.body, "a", "Open Runtime")?.getAttribute("href")).toBe(
      `/projects/${PROJECT_ID}/configuration/runtime`,
    );
    await view.unmount();
  });

  it("drops a failed import's error when the gallery opens again", async () => {
    stubSectionApi(409);
    const view = await mountSectionWith(sectionClient(), LANES);
    await click(byText(document.body, "button", "Create the branch"));
    await importSimpleLane();
    expect(document.body.textContent).toContain("already has a factorial branch");

    await click(byText(document.body, "button", "Create the branch"));
    expect(document.querySelectorAll("[data-gallery-card]")).not.toHaveLength(0);
    expect(document.body.textContent).not.toContain("already has a factorial branch");
    await view.unmount();
  });

  it("shows the seed's 409 and re-reads the branch", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.endsWith("/config-branch"))
          return jsonResponse(
            { detail: "northwind/harbor-invoices already has a factorial branch" },
            409,
          );
        return new Promise<Response>(() => {});
      }),
    );
    const qc = sectionClient();
    const view = await mountSectionWith(qc);
    await importSimpleLane();

    expect(document.body.textContent).toContain("already has a factorial branch");
    expect(qc.getQueryState(keys.projectReadiness(PROJECT_ID))?.isInvalidated).toBe(true);
    expect(document.querySelectorAll("textarea")).toHaveLength(0);
    await view.unmount();
  });

  it("keeps a new lane's chosen name when the editor's template replaces its text", async () => {
    stubEditorApi();
    const view = await mount(
      sectionTree({
        stale: [],
        laneless: true,
        initialText: SIMPLE_LANE.yaml.replace("name: simple", "name: simple-2"),
        initialTitle: "Simple lane",
      }),
    );
    await settle(700);
    await settle();
    await click(await openTemplateItem());

    const texts = [...document.querySelectorAll("textarea")].map((area) => area.value);
    expect(texts.some((text) => text.includes("\nname: simple-2\n"))).toBe(true);
    expect(texts.some((text) => text.includes("\nname: simple\n"))).toBe(false);
    await view.unmount();
  });

  it("starts a gallery pick over a leftover new-lane draft, with the draft one Undo away", async () => {
    writeDraft(PROJECT_ID, null, null, "name: leftover\n");
    stubEditorApi();
    const view = await mount(
      <>
        {sectionTree({
          stale: [],
          laneless: true,
          initialText: "name: simple-2\n",
          initialTitle: "Simple lane",
        })}
        <FlagGroup />
      </>,
    );
    await settle(700);
    await settle();

    expect(
      [...document.querySelectorAll("textarea")].some((area) => area.value === "name: simple-2\n"),
    ).toBe(true);
    expect(document.body.textContent).toContain(
      "Replaced your unsaved new-process draft with Simple lane",
    );
    await view.unmount();
    clearFlags();
  });
});

/** Closing the editor brings the section back to where a reader can see it. */
describe("ProcessSection, when the editor closes", () => {
  async function openThenClose(top: number) {
    stubEditorApi();
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    holding(qc);
    qc.setQueryData(keys.processTemplates, { templates: [SIMPLE_TEMPLATE] });
    qc.setQueryData(keys.runtimes, []);
    const view = await mount(
      <QueryClientProvider client={qc}>
        <div id="readiness-process_config">
          <MemoryRouter>
            <ProcessSection
              project={NO_LANES}
              branch={branchItem("ok", "factorial · no force-push or deletion")}
            />
          </MemoryRouter>
        </div>
      </QueryClientProvider>,
    );
    const anchor = document.getElementById("readiness-process_config") as HTMLElement;
    const scroll = vi.fn();
    anchor.scrollIntoView = scroll;
    anchor.getBoundingClientRect = () => ({ top }) as DOMRect;
    await click(byText(document.body, "button", "Edit config"));
    await settle(50);
    await click(byText(document.body, "button", "Back"));
    await settle(300);
    await view.unmount();
    return scroll;
  }

  it("scrolls the section's top into view when the page left it above the screen", async () => {
    const scroll = await openThenClose(-600);
    expect(scroll).toHaveBeenCalledTimes(1);
    expect(scroll.mock.calls[0]?.[0]).toMatchObject({ block: "start" });
  });

  it("leaves the page alone when the section's top is already in view", async () => {
    expect(await openThenClose(40)).not.toHaveBeenCalled();
  });
});

describe("ProcessSection, with a drafted new lane", () => {
  it("reopens the editor on the draft rather than on the lane that runs", async () => {
    writeDraft(PROJECT_ID, null, null, "specVersion: factorial/v1\nkind: Process\nname: hotfix\n");
    stubEditorApi();
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    holding(qc);
    qc.setQueryData(keys.processTemplates, { templates: [SIMPLE_TEMPLATE] });
    qc.setQueryData(keys.runtimes, []);
    const view = await mount(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ProcessSection
            project={
              { ...PROJECT, settings: { runtime_allowlist: [] } } as unknown as ProjectDetail
            }
            branch={branchItem("ok", "factorial · no force-push or deletion")}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await click(byText(document.body, "button", "Edit and finalize new process"));
    await settle(50);
    expect(
      [...document.querySelectorAll("textarea")].some((area) =>
        area.value.includes("name: hotfix"),
      ),
    ).toBe(true);
    await view.unmount();
  });
});

describe("Opening a skill's or MCP server's file", () => {
  it("opens on the file it was asked for rather than the process file", async () => {
    stubEditorApi();
    const path = ".factorial/agents/plan-agent/agent.yaml";
    const view = await mount(
      sectionTree({
        stale: [],
        initialFile: "agents/plan-agent/agent.yaml",
        fileBodies: { [path]: "kind: Agent\nname: plan-agent\n" },
      }),
    );
    await settle(700);
    await settle();
    expect(textareaFor(path)?.value).toBe("kind: Agent\nname: plan-agent\n");
    await view.unmount();
  });

  it("a chip opens the editor on its file", async () => {
    stubEditorApi();
    const path = ".factorial/skills/project-constitution/SKILL.md";
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    holding(qc);
    qc.setQueryData(keys.processTemplates, { templates: [SIMPLE_TEMPLATE] });
    qc.setQueryData(keys.runtimes, []);
    qc.setQueryData(keys.processVersion(PROJECT_ID, LATEST_ID), {
      project_id: PROJECT_ID,
      process_key: PROCESS_KEY,
      source_commit_sha: CURRENT_SHA,
      content: { ...CONFIG, steps: [{ ...CONFIG.steps[0], agent: "plan-agent" }, CONFIG.steps[1]] },
    });
    qc.setQueryData(keys.processVersionRaw(PROJECT_ID, LATEST_ID), {
      content: RAW_TEXT,
      source: "repo",
    });
    qc.setQueryData(keys.processVersionAgents(PROJECT_ID, LATEST_ID), [
      {
        name: "plan-agent",
        warning: null,
        model: "claude-haiku-4-5-20251001",
        max_turns: 40,
        skills_local: [],
        skills_shared: ["project-constitution"],
        mcp_local: [],
        mcp_shared: [],
        secrets: [],
        permissions: null,
      },
    ]);
    qc.setQueryData(keys.configFiles(PROJECT_ID, LATEST_ID), {
      config_dir: ".factorial",
      pinned_sha: CURRENT_SHA,
      head_sha: CURRENT_SHA,
      files: [{ path, kind: "skill", drift: "same", warning: null, sha: "a".repeat(40) }],
    });
    qc.setQueryData(keys.configFile(PROJECT_ID, LATEST_ID, path), {
      path,
      content: "# Constitution\n",
    });
    const view = await mount(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ProcessSection
            project={
              { ...PROJECT, settings: { runtime_allowlist: [] } } as unknown as ProjectDetail
            }
            branch={branchItem("ok", "factorial · no force-push or deletion")}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const chip = [...document.querySelectorAll("button")].find((button) =>
      button.textContent?.startsWith("project-constitution"),
    );
    await click(chip);
    await settle(50);
    expect(textareaFor(path)?.value).toBe("# Constitution\n");
    await view.unmount();
  });
});

/** The builder commits to the branch: with none, or one it will not build on, nothing opens an
 *  editor whose commit would be refused. */
describe("ProcessSection, when the branch cannot take a commit", () => {
  it("opens no editor from Edit config at an unknown head", async () => {
    stubEditorApi();
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    holding(qc);
    qc.setQueryData(keys.runtimes, []);
    const view = await mount(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ProcessSection project={NO_LANES} branch={branchItem("warn", "unknown head eeeeeee")} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await click(byText(document.body, "button", "Edit config"));
    await settle(50);
    expect(document.querySelector("textarea")).toBeNull();
    await view.unmount();
  });

  it("turns Sync config into Review and adopt, which opens the review", async () => {
    stubEditorApi();
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    holding(qc);
    qc.setQueryData(keys.runtimes, []);
    const view = await mount(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ProcessSection
            project={NO_LANES}
            branch={branchItem("warn", "unknown head eeeeeee", {
              remedy: "git push --force-with-lease origin aaaaaaa:refs/heads/factorial",
            })}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(byText(document.body, "button", "Sync config")).toBeNull();
    await click(byText(document.body, "button", "Review and adopt"));
    await settle(50);
    expect(document.querySelector('[role="dialog"]')?.textContent).toContain("factorial");
    await view.unmount();
  });

  it("syncs the factorial branch itself once the head is known, without the review", async () => {
    const synced: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (url.endsWith("/process-versions/sync")) synced.push(JSON.parse(String(init?.body)));
        return jsonResponse({ detail: "not part of this test" }, 404);
      }),
    );
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    holding(qc);
    qc.setQueryData(keys.runtimes, []);
    const view = await mount(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ProcessSection
            project={NO_LANES}
            branch={branchItem("ok", "factorial · no force-push or deletion")}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await click(byText(document.body, "button", "Sync config"));
    await settle(50);
    expect(synced).toEqual([{ ref: null }]);
    expect(document.body.textContent).not.toContain("moved to a commit no one has reviewed");
    expect(document.body.textContent).toContain("not part of this test");
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    await view.unmount();
  });

  it("offers Review and adopt on the warning when a known commit lies beneath", () => {
    const html = render(
      PROJECT,
      branchItem("warn", "unknown head eeeeeee", {
        remedy: "git push --force-with-lease origin aaaaaaa:refs/heads/factorial",
      }),
    );
    expect(html).toContain("Review and adopt");
    expect(html).toContain("Command to reset the factorial branch");
    expect(html).not.toContain("Copy reset command");
  });

  it("offers Review and adopt when no known commit lies beneath", () => {
    const html = render(PROJECT, branchItem("warn", "unknown head eeeeeee"));
    expect(html).toContain("Review and adopt");
    expect(html).not.toContain("Command to reset the factorial branch");
  });

  it("says one thing at an unknown head with no process: one alert, no Edit config advice", async () => {
    stubEditorApi();
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    holding(qc);
    qc.setQueryData(keys.runtimes, []);
    const view = await mount(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ProcessSection
            project={NO_LANES}
            branch={branchItem("warn", "unknown head eeeeeee", {
              detail: "Another Factorial install wrote it.",
            })}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const text = document.body.textContent ?? "";
    expect(text).toContain("Review the factorial branch before Factorial syncs it");
    expect(text).toContain("No process is synced yet. Another Factorial install wrote it.");
    expect(text).toContain("No process yet.");
    expect(text).not.toContain("Edit config to write one");
    expect(text).not.toContain("moved to a commit no one has reviewed");
    expect(text).not.toContain("never synced");
    expect(byText(document.body, "button", "Review and adopt")).not.toBeNull();
    expect(document.querySelectorAll("[role=alert]")).toHaveLength(0);
    await view.unmount();
  });

  /** A project whose branch row is `ok` when the sync is pressed, then `warn` once the probe lands. */
  async function syncRefused(status: number, body: unknown, after: ReadinessItem) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(body, status)),
    );
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    holding(qc);
    qc.setQueryData(keys.runtimes, []);
    const tree = (branch: ReadinessItem) => (
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ProcessSection project={NO_LANES} branch={branch} />
        </MemoryRouter>
      </QueryClientProvider>
    );
    const view = await mount(tree(branchItem("ok", "factorial · no force-push or deletion")));
    await click(byText(document.body, "button", "Sync config"));
    await settle(50);
    await view.rerender(tree(after));
    return view;
  }

  it("does not say an unknown head twice once the probe catches up with the refused sync", async () => {
    const view = await syncRefused(
      422,
      { detail: "The head is not reviewed.", code: "unknown_head" },
      branchItem("warn", "unknown head eeeeeee", { detail: "Another Factorial install wrote it." }),
    );
    expect(document.body.textContent).not.toContain("The head is not reviewed.");
    expect(document.body.textContent).not.toContain("moved to a commit no one has reviewed");
    await view.unmount();
  });

  it("links a missing runtime to the form that creates it", async () => {
    const view = await syncRefused(
      422,
      {
        detail:
          "Sync refused: lane 'standard' names runtime 'claude-api-on-e2b', which does not exist.",
        code: "runtime_missing",
        missing_runtimes: ["claude-api-on-e2b", "codex-api-on-e2b"],
      },
      branchItem("ok", "factorial · no force-push or deletion"),
    );
    expect(document.body.textContent).toContain("which does not exist");
    const link = [...document.querySelectorAll("a")].find(
      (a) => a.textContent === "Add the runtime",
    );
    expect(link?.getAttribute("href")).toContain("/runtimes/new?slug=claude-api-on-e2b");
    await view.unmount();
  });

  it("still shows a refusal that has no code, such as a YAML error", async () => {
    const view = await syncRefused(
      422,
      { detail: "lane 'standard' is not valid YAML." },
      branchItem("ok", "factorial · no force-push or deletion"),
    );
    expect(document.body.textContent).toContain("lane 'standard' is not valid YAML.");
    await view.unmount();
  });

  it("leaves the skill and MCP chips as plain labels", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    holding(qc);
    qc.setQueryData(keys.processVersion(PROJECT_ID, LATEST_ID), {
      project_id: PROJECT_ID,
      process_key: PROCESS_KEY,
      source_commit_sha: CURRENT_SHA,
      content: { ...CONFIG, steps: [{ ...CONFIG.steps[0], agent: "plan-agent" }, CONFIG.steps[1]] },
    });
    qc.setQueryData(keys.processVersionAgents(PROJECT_ID, LATEST_ID), [
      {
        name: "plan-agent",
        warning: null,
        model: "claude-haiku-4-5-20251001",
        max_turns: 40,
        skills_local: [],
        skills_shared: ["project-constitution"],
        mcp_local: [],
        mcp_shared: [],
        secrets: [],
        permissions: null,
      },
    ]);
    const html = renderToString(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ProcessSection project={PROJECT} branch={branchItem("warn", "missing")} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(html).toContain("project-constitution");
    expect(html).not.toMatch(/<button[^>]*>(?:<[^>]+>)*project-constitution/);
  });
});

describe("ProcessSection, for a reader who may not change the config", () => {
  const AGENT = ".factorial/agents/plan-agent/agent.yaml";
  const PROMPT = ".factorial/agents/plan-agent/system-prompt.md";

  async function viewer(permissions: string[]) {
    const calls: Array<{ url: string; method: string }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({ url, method: init?.method ?? "GET" });
        return jsonResponse({ detail: "not part of this test" }, 404);
      }),
    );
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    holding(qc, permissions);
    qc.setQueryData(keys.processTemplates, { templates: [SIMPLE_TEMPLATE] });
    qc.setQueryData(keys.processVersion(PROJECT_ID, LATEST_ID), {
      project_id: PROJECT_ID,
      process_key: PROCESS_KEY,
      source_commit_sha: CURRENT_SHA,
      content: CONFIG,
    });
    qc.setQueryData(keys.processVersionRaw(PROJECT_ID, LATEST_ID), {
      content: RAW_TEXT,
      source: "repo",
    });
    qc.setQueryData(keys.processVersionAgents(PROJECT_ID, LATEST_ID), []);
    qc.setQueryData(keys.configFiles(PROJECT_ID, LATEST_ID), {
      config_dir: ".factorial",
      pinned_sha: CURRENT_SHA,
      head_sha: CURRENT_SHA,
      files: [AGENT, PROMPT].map((path) => ({
        path,
        kind: "agent",
        drift: "same",
        warning: null,
        sha: "a".repeat(40),
      })),
    });
    qc.setQueryData(keys.configFile(PROJECT_ID, LATEST_ID, PROMPT), {
      path: PROMPT,
      content: "You plan the work.\n",
    });
    const view = await mount(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ProcessSection
            project={{ ...PROJECT, settings: { runtime_allowlist: [] } } as ProjectDetail}
            branch={branchItem("ok", "factorial · no force-push or deletion")}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    return { view, calls };
  }

  it("opens the config read-only: every file to read, nothing to change, nothing sent or kept", async () => {
    // A manager's draft of the lane is theirs: the reader neither sees it nor clears it.
    writeDraft(PROJECT_ID, PROCESS_KEY, CURRENT_SHA, "name: drafted\n");
    const stored = Object.keys(localStorage).sort();
    const { view, calls } = await viewer(MEMBER_PERMISSIONS);
    expect(byText(document.body, "button", "Edit config")).toBeNull();
    await click(byText(document.body, "button", "View config"));
    await settle(50);

    expect(textareaFor("process.yaml")?.value).toBe(RAW_TEXT);
    expect(textareaFor("process.yaml")?.readOnly).toBe(true);
    expect(await treeFile("agent.yaml")).not.toBeNull();
    await click(await treeFile("system-prompt.md"));
    await settle(50);
    expect(textareaFor(PROMPT)?.value).toBe("You plan the work.\n");
    expect(textareaFor(PROMPT)?.readOnly).toBe(true);

    for (const label of [
      "Commit and sync",
      "No changes to commit",
      "Permit and commit",
      "Discard edits",
      "Import",
    ]) {
      expect(byText(document.body, "button", label)).toBeNull();
    }
    expect(document.querySelector('[aria-label="New file"]')).toBeNull();
    expect(document.querySelector('[aria-label="New folder"]')).toBeNull();
    expect(document.querySelector('input[type="checkbox"]')).toBeNull();
    expect(document.querySelector('input[type="file"]')).toBeNull();

    await new Promise((resolve) => setTimeout(resolve, 700));
    expect(calls.filter((call) => call.method !== "GET")).toEqual([]);
    expect(Object.keys(localStorage).sort()).toEqual(stored);
    expect(readDraftText()).toBe("name: drafted\n");
    await view.unmount();
    localStorage.clear();
  });

  it("keeps a manager's editor as it was", async () => {
    const { view } = await viewer(ADMIN_PERMISSIONS);
    expect(byText(document.body, "button", "View config")).toBeNull();
    await click(byText(document.body, "button", "Edit config"));
    await settle(50);
    expect(textareaFor("process.yaml")?.readOnly).toBe(false);
    expect(byText(document.body, "button", "Import")).not.toBeNull();
    expect(document.querySelector('[aria-label="New file"]')).not.toBeNull();
    await view.unmount();
    localStorage.clear();
  });

  function readDraftText(): string | null {
    const raw = localStorage.getItem(`sf.processDraft.${PROJECT_ID}.${PROCESS_KEY}`);
    return raw === null ? null : JSON.parse(raw).text;
  }
});
