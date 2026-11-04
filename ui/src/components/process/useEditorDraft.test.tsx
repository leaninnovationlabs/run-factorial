// @vitest-environment happy-dom
/**
 * The process editor opening on an assistant's draft from `?draft=`: the draft becomes the
 * editor's stored draft, a draft the person already holds is replaced only after a confirm, and
 * the address forgets the draft so a reload does not load it again.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { ApiError, api } from "../../api/client";
import { keys } from "../../api/keys";
import { holding } from "../../api/organization.fixture";
import type { EditorDraftPayload, ProjectDetail } from "../../api/types";
import { byText, click, type Mounted, mount, settle } from "../../testDom";
import { readBundleDraft } from "./explorer/bundleDraft";
import { readRepoEdits } from "./explorer/repoEdits";
import { ProcessSection } from "./ProcessSection";
import { readDraft, writeDraft } from "./processDraft";

const SHA = "a".repeat(40);
const RUNNING = "specVersion: factorial/v1\nname: standard\n";

const PROJECT = {
  id: 51,
  name: "api",
  github_repo: "acme/api",
  description: null,
  active: true,
  ticket_source: null,
  settings: { runtime_allowlist: [] },
  processes: [
    {
      process_key: "standard",
      is_default: true,
      selection_label: "factorial:process:standard",
      latest: {
        id: 30,
        project_id: 51,
        process_key: "standard",
        source_commit_sha: SHA,
        retired_at: null,
        source_ref: "factorial",
        activated_at: "2026-10-01T09:00:00Z",
        created_at: "2026-10-01T09:00:00Z",
      },
    },
  ],
  default_process: "standard",
  blockers: [],
  warnings: [],
  readiness: [],
} as unknown as ProjectDetail;

const DRAFT: EditorDraftPayload = {
  project_id: 51,
  process: "standard",
  content: `${RUNNING}# a second reviewer on the spec gate\n`,
  files: [
    { path: ".factorial/skills/review/SKILL.md", body: "# Review\n" },
    { path: ".factorial/mcp/linear.yaml", body: null },
  ],
  edits: [
    { path: ".factorial/agents/build/prompt.md", body: "Build it.\n", base_sha: "b".repeat(40) },
  ],
  base_sha: SHA,
  would_write: [],
};

let view: Mounted | null = null;

beforeEach(() => {
  localStorage.clear();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      url.endsWith("/process-versions/validate")
        ? new Response(JSON.stringify({ valid: true, errors: [], warnings: [], content: {} }))
        : new Promise<Response>(() => {}),
    ),
  );
});

afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
});

function Where() {
  const { pathname, search } = useLocation();
  return <output data-where>{`${pathname}${search}`}</output>;
}

async function open(draft: EditorDraftPayload | ApiError = DRAFT) {
  if (draft instanceof ApiError) vi.spyOn(api, "getAssistantDraft").mockRejectedValue(draft);
  else vi.spyOn(api, "getAssistantDraft").mockResolvedValue(draft);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  holding(client);
  client.setQueryData(keys.processTemplates, { templates: [] });
  client.setQueryData(keys.runtimes, []);
  client.setQueryData(keys.processVersionRaw(51, 30), { content: RUNNING, source: "repo" });
  view = await mount(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/projects/51/configuration/process?draft=41"]}>
        <Where />
        <ProcessSection project={PROJECT} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await settle();
  await settle();
  return view;
}

const where = () => document.querySelector("[data-where]")?.textContent;
const editor = () =>
  [...document.querySelectorAll("h2")].find((heading) =>
    heading.textContent?.startsWith("Edit config"),
  ) ?? null;

describe("useEditorDraft", () => {
  test("?draft= loads the assistant's draft into the editor's stored draft, then leaves the address", async () => {
    await open();
    expect(api.getAssistantDraft).toHaveBeenCalledWith(41);
    expect(editor()).not.toBeNull();
    expect(where()).toBe("/projects/51/configuration/process");
    // Past the text draft's debounce.
    await settle(500);
    expect(readDraft(51, "standard")?.text).toBe(DRAFT.content);
    expect(readRepoEdits(51, "standard")).toEqual({
      ".factorial/agents/build/prompt.md": { body: "Build it.\n", base: "b".repeat(40) },
    });
    expect(readBundleDraft(51, "standard")).toEqual({
      ".factorial/skills/review/SKILL.md": { body: "# Review\n", facts: {} },
    });
    expect(document.body.textContent).not.toContain("written against");
  });

  test("a draft written against another commit opens with a warning that says so", async () => {
    await open({ ...DRAFT, base_sha: "c".repeat(40) });
    expect(editor()).not.toBeNull();
    const warning = byText(
      document.body,
      "h3",
      "The assistant's draft was written against another commit",
    );
    expect(warning).not.toBeNull();
    expect(warning?.closest("section")?.textContent).toContain("ccccccc");
    expect(warning?.closest("section")?.textContent).toContain("aaaaaaa");
  });

  test("with unsaved edits a confirm comes first: Cancel keeps them, Replace takes the draft", async () => {
    writeDraft(51, "standard", SHA, `${RUNNING}# mine\n`);
    await open();
    expect(editor()).toBeNull();
    const confirm = () => document.querySelector<HTMLElement>('[role="dialog"]');
    expect(confirm()?.textContent).toContain("Replace your unsaved edits to standard?");
    await click(byText(confirm() ?? document.body, "button", "Cancel"));
    await settle(250);
    expect(where()).toBe("/projects/51/configuration/process");
    expect(editor()).toBeNull();
    expect(readDraft(51, "standard")?.text).toBe(`${RUNNING}# mine\n`);
    await view?.unmount();

    await open();
    await click(byText(confirm() ?? document.body, "button", "Replace with the draft"));
    await settle(250);
    expect(editor()).not.toBeNull();
    expect(where()).toBe("/projects/51/configuration/process");
    await settle(500);
    expect(readDraft(51, "standard")?.text).toBe(DRAFT.content);
  });

  test("a draft of a lane the project lacks opens the editor on a new lane", async () => {
    await open({ ...DRAFT, process: "hotfix", content: "name: hotfix\n" });
    expect(editor()).not.toBeNull();
    await settle(500);
    expect(readDraft(51, null)?.text).toBe("name: hotfix\n");
    expect(readDraft(51, "standard")).toBeNull();
  });

  test("a draft of another project opens nothing, says so, and leaves the address", async () => {
    await open({ ...DRAFT, project_id: 52 });
    expect(editor()).toBeNull();
    expect(document.body.textContent).toContain("That draft is for another project");
    expect(where()).toBe("/projects/51/configuration/process");
  });

  test("a draft the API cannot give says why, and leaves the address", async () => {
    await open(new ApiError(404, "no such draft"));
    expect(editor()).toBeNull();
    expect(document.body.textContent).toContain("no such draft");
    expect(where()).toBe("/projects/51/configuration/process");
  });
});
