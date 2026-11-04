// @vitest-environment happy-dom
/**
 * One configuration page: which of them run the repo-side probes on arrival, the page an address
 * nobody built gets, the pages that hold several requirements: Git Source, Tickets, Runtime, and
 * what a reader who may not change the config is left with.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { UNREACHABLE } from "../api/client";
import { ADMIN_PERMISSIONS, holding, MEMBER_PERMISSIONS } from "../api/organization.fixture";
import { keys } from "../api/queries";
import type { ProjectDetail, ReadinessCheck, ReadinessItem } from "../api/types";
import { APP_PREFIX } from "../prefix";
import { byText, choose, click, type Mounted, mount, settle, type } from "../testDom";
import { ProjectConfigurationSectionPage } from "./ProjectConfigurationSectionPage";
import { PROJECT_ID, project } from "./project.fixture";

const fetchMock = vi.fn();
let view: Mounted | null = null;

beforeEach(() => {
  fetchMock.mockImplementation(() => Promise.reject(new TypeError("offline")));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(async () => {
  await view?.unmount();
  view = null;
  fetchMock.mockReset();
  vi.unstubAllGlobals();
});

async function page(
  section: string,
  over: Partial<ProjectDetail> = {},
  permissions = ADMIN_PERMISSIONS,
): Promise<HTMLElement> {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Number.POSITIVE_INFINITY } },
  });
  holding(qc, permissions);
  qc.setQueryData(keys.project(PROJECT_ID), project(over));
  // The list refuses anyone else, so only such a reader's page has it to read.
  if (permissions.includes("runtimes:manage")) qc.setQueryData(keys.runtimes, []);
  view = await mount(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/projects/${PROJECT_ID}/configuration/${section}`]}>
        <Routes>
          <Route
            path="/projects/:projectId/configuration/:section"
            element={<ProjectConfigurationSectionPage />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await settle();
  return view.container;
}

const paths = () =>
  fetchMock.mock.calls.map(([url]) =>
    new URL(String(url), "http://api").pathname.slice(APP_PREFIX.length),
  );

const SECRETS = {
  key: "secrets" as const,
  process: null,
  runtime_slug: null,
  state: "ok" as const,
  summary: "every declared secret is stored",
  detail: null,
  remedy: null,
  link: null,
};

describe("a configuration page", () => {
  it("runs the repo-side probes when its own row is one of them", async () => {
    const root = await page("repository-setup");
    expect(root.querySelector("h1")?.textContent).toBe("Git source");
    expect(paths()).toContain(`/api/projects/${PROJECT_ID}/readiness`);
    // Nothing answered, and the page says so rather than leaving the row "not checked".
    expect(root.textContent).toContain("Couldn't check the repository");
  });

  it("reads nothing from the repository for a row the project's own payload answers", async () => {
    const root = await page("secrets", { readiness: [SECRETS] });
    expect(root.querySelector("h1")?.textContent).toBe("Secrets");
    expect(root.textContent).toContain("Ready");
    expect(root.textContent).toContain("every declared secret is stored");
    expect(paths()).not.toContain(`/api/projects/${PROJECT_ID}/readiness`);
  });

  it("names itself and nothing above it: the side navigation says where it is", async () => {
    const root = await page("details");
    expect(root.querySelector("h1")?.textContent).toBe("Details");
    expect(root.querySelector('nav[aria-label="Breadcrumbs"]')).toBeNull();
  });

  it("says so for a section nobody built, with the way back to the overview", async () => {
    const root = await page("plumbing");
    expect(root.querySelector("h1")?.textContent).toBe("Page not found");
    expect(root.querySelector(`a[href="/projects/${PROJECT_ID}/configuration"]`)).not.toBeNull();
    expect(paths()).not.toContain(`/api/projects/${PROJECT_ID}/readiness`);
  });
});

const OUT_OF_REACH: ReadinessItem = {
  key: "app_access",
  process: null,
  runtime_slug: null,
  state: "blocked",
  summary: "northwind/harbor-invoices is not in reach",
  detail: "the App is installed on northwind but cannot see northwind/harbor-invoices",
  remedy: null,
  link: "https://github.com/organizations/northwind/settings/installations/1",
};

function answer(readiness: ReadinessItem[]) {
  fetchMock.mockImplementation((url: string, init?: RequestInit) => {
    const path = new URL(String(url), "http://api").pathname;
    const json = (body: unknown) =>
      Promise.resolve(
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    if (path.endsWith("/readiness")) return json({ readiness, warnings: [] });
    if (init?.method === "PATCH") return json(project(JSON.parse(String(init.body))));
    if (init?.method === "PUT") return json({ ...SOURCE, ...JSON.parse(String(init.body)) });
    return Promise.reject(new TypeError("offline"));
  });
}

const SOURCE = {
  id: 1,
  project_id: PROJECT_ID,
  kind: "github",
  external_ref: "northwind/harbor-invoices",
  webhook_secret_ref: null,
  last_polled_at: null,
  created_at: "2026-10-01T00:00:00Z",
};

/** The control a label names, matched whole: "Repository" is not "Repository host". */
function field(root: HTMLElement, label: string): HTMLInputElement | null {
  const tag = [...root.querySelectorAll("label")].find(
    (l) => l.textContent?.replace(/\s*\*$/, "").trim() === label,
  );
  return tag ? (document.getElementById(tag.htmlFor) as HTMLInputElement | null) : null;
}

const UNPROTECTED: ReadinessItem = {
  key: "branch_protection",
  process: null,
  runtime_slug: null,
  state: "warn",
  summary: "main is not protected",
  detail: null,
  remedy: "gh api -X PUT repos/northwind/harbor-invoices/branches/main/protection --input -",
  link: null,
};

const check = (key: string, title: string, passed: boolean, fix: Partial<ReadinessCheck> = {}) => ({
  key,
  title,
  description: `${title}: what it holds`,
  passed,
  remedy: null,
  link: null,
  ...fix,
});

const PROTECTION_CHECKED: ReadinessItem = {
  ...UNPROTECTED,
  summary: "main: stale approvals not dismissed",
  remedy: null,
  checks: [
    check("protected", "Has a protection rule", true),
    {
      // The server sends a passing check's command too; the page keeps it to itself.
      ...check("approval", "Requires an approving review", true, {
        remedy: "gh api -X PATCH … -F required_approving_review_count=1",
      }),
      description: "A pull request needs one approval before it merges.",
    },
    check("dismiss_stale", "Dismisses stale approvals", false, {
      remedy:
        "gh api -X PATCH repos/northwind/harbor-invoices/branches/main/protection/required_pull_request_reviews -F dismiss_stale_reviews=true",
    }),
  ],
};

const CONFIG_RULES: ReadinessItem = {
  ...UNPROTECTED,
  key: "config_branch_rules",
  state: "warn",
  summary: "factorial: pull_request stops the builder's commits",
  remedy: null,
  checks: [
    check("no_force_push", "Blocks force-push", true),
    check("no_deletion", "Blocks deletion", true),
    check("builder_can_commit", "Lets the builder commit", false, {
      link: "https://github.com/northwind/harbor-invoices/settings/rules",
    }),
  ],
};

function patches() {
  return fetchMock.mock.calls
    .filter(([, init]) => init?.method === "PATCH")
    .map(([, init]) => JSON.parse(String(init?.body)));
}

describe("Process", () => {
  it("speaks once for an unknown head, not again for the config never being synced", async () => {
    answer([
      {
        ...UNPROTECTED,
        key: "config_branch",
        summary: "unknown head eeeeeee",
        detail: "Another Factorial install wrote it.",
      },
    ]);
    const root = await page("process", {
      readiness: [
        {
          ...UNPROTECTED,
          key: "process_config",
          state: "blocked",
          summary: "never synced",
          detail: "No process yet. Sync one from the repository.",
        },
      ],
    });
    await settle(20);
    expect(root.textContent).toContain("Review the factorial branch before Factorial syncs it");
    expect(root.textContent).not.toContain("never synced");
    expect(root.textContent).not.toContain("Sync one from the repository");
  });
});

describe("Details", () => {
  it("is the name and a description in a text area, and nothing else", async () => {
    answer([]);
    const root = await page("details");
    expect(field(root, "Name")).not.toBeNull();
    expect(field(root, "Description")?.tagName).toBe("TEXTAREA");
    expect(field(root, "Repository URL")).toBeNull();
    expect(paths()).not.toContain(`/api/projects/${PROJECT_ID}/readiness`);
  });

  it("saves only what changed", async () => {
    answer([]);
    const root = await page("details");
    expect(byText(root, "button", "Save details")?.hasAttribute("disabled")).toBe(true);
    await type(field(root, "Description"), "Invoices for the harbor office");
    await click(byText(root, "button", "Save details"));
    await settle(20);
    expect(patches()).toEqual([{ description: "Invoices for the harbor office" }]);
  });
});

describe("Git Source", () => {
  it("says a repository the App cannot see under its URL, with the fix as a button", async () => {
    answer([OUT_OF_REACH]);
    const root = await page("repository-setup");
    await settle(20);
    expect(paths()).toContain(`/api/projects/${PROJECT_ID}/readiness`);
    expect(root.textContent).toContain("Cannot run");
    const note = root.querySelector("[data-access-note]");
    expect(note?.closest("form")).not.toBeNull();
    expect(note?.textContent).toContain("cannot see northwind/harbor-invoices");
    const fix = note?.querySelector(`a[href="${OUT_OF_REACH.link}"]`);
    expect(fix?.getAttribute("data-appearance")).toBe("default");
    // Said once: the box at the top leaves it to the note.
    expect(root.querySelector("[data-issue]")).toBeNull();
    expect([...root.querySelectorAll("h3")].map((h) => h.textContent)).not.toContain(
      "GitHub App access",
    );
  });

  it("says nothing about access when the App can reach the repository", async () => {
    answer([{ ...OUT_OF_REACH, state: "ok", summary: "installed", detail: null }]);
    const root = await page("repository-setup");
    await settle(20);
    expect(root.querySelector("[data-access-note]")).toBeNull();
    expect(root.textContent).not.toContain("The App must be installed");
  });

  it("puts every other problem on the page in the one box, leaving the branch rules to their checks", async () => {
    answer([
      OUT_OF_REACH,
      UNPROTECTED,
      { ...CONFIG_RULES, state: "warn", summary: "factorial: force-push allowed" },
      { ...UNPROTECTED, key: "ci_workflows", summary: "no workflows", remedy: null },
    ]);
    const root = await page("repository-setup");
    await settle(20);
    expect(root.querySelectorAll("section:has([data-issue])")).toHaveLength(1);
    expect(
      [...root.querySelectorAll("[data-issue]")].map((row) => row.getAttribute("data-issue")),
    ).toEqual(["ci_workflows-"]);
  });

  it("numbers each branch and draws its rules check by check, a command beside each failure", async () => {
    answer([PROTECTION_CHECKED, CONFIG_RULES]);
    const root = await page("repository-setup");
    await settle(20);
    expect([...root.querySelectorAll("h4")].map((h) => h.textContent)).toEqual([
      "1. Default branch",
      "2. Config branch factorial",
    ]);
    const check = (key: string) => root.querySelector(`[data-check="${key}"]`);
    expect(check("approval")?.textContent).toContain("Requires an approving review");
    expect(check("approval")?.textContent).toContain("needs one approval");
    // A passing check says what it holds and offers nothing to run.
    expect(byText(check("approval") as HTMLElement, "button", "Show command")).toBeNull();

    const failing = check("dismiss_stale") as HTMLElement;
    const copy = byText(failing, "button", "Copy");
    expect(copy?.getAttribute("aria-label")).toBe("Copy Dismisses stale approvals");
    const toggle = byText(failing, "button", "Show command");
    const code = document.getElementById(toggle?.getAttribute("aria-controls") ?? "");
    expect(code?.hidden).toBe(true);
    await click(toggle);
    expect(code?.textContent).toContain("-F dismiss_stale_reviews=true");

    // A rule that has to come out of someone's ruleset is a way there, not a command.
    const blocked = check("builder_can_commit") as HTMLElement;
    expect(byText(blocked, "button", "Show command")).toBeNull();
    expect(blocked.querySelector("a")?.getAttribute("href")).toBe(
      "https://github.com/northwind/harbor-invoices/settings/rules",
    );
  });

  it("shows the branch rules only while the form holds the saved repository", async () => {
    answer([PROTECTION_CHECKED, CONFIG_RULES]);
    const root = await page("repository-setup");
    await settle(20);
    const shown = () =>
      [...root.querySelectorAll("h3")].some((h) => h.textContent === "Branch protection");
    expect(shown()).toBe(true);
    await type(field(root, "Repository URL"), "");
    expect(shown()).toBe(false);
    await type(field(root, "Repository URL"), "https://github.com/northwind/harbor-ledger");
    expect(shown()).toBe(false);
    await type(field(root, "Repository URL"), "https://github.com/northwind/harbor-invoices");
    expect(shown()).toBe(true);
  });

  it("numbers from what it shows, when a process ignores the default branch", async () => {
    answer([CONFIG_RULES]);
    const root = await page("repository-setup");
    await settle(20);
    expect([...root.querySelectorAll("h4")].map((h) => h.textContent)).toEqual([
      "1. Config branch factorial",
    ]);
  });

  it("shows the host the ticket source set, and offers no choice of its own", async () => {
    answer([]);
    const root = await page("repository-setup");
    const host = field(root, "Repository host") as unknown as HTMLSelectElement | null;
    expect(host?.value).toBe("github");
    expect(host?.disabled).toBe(true);
  });

  it("takes a GitLab project's nested path as saved, so its branch checks show", async () => {
    answer([UNPROTECTED]);
    const root = await page("repository-setup", {
      github_repo: "harbor/finance/invoices",
      ticket_source: { ...SOURCE, kind: "gitlab", external_ref: "harbor/finance/invoices" },
    });
    const host = field(root, "Repository host") as unknown as HTMLSelectElement | null;
    expect(host?.value).toBe("gitlab");
    expect(field(root, "GitLab project")?.value).toBe("harbor/finance/invoices");
    expect(root.textContent).not.toContain("Use group/project");
    expect(root.querySelector('[data-branch-checks="branch_protection"]')).not.toBeNull();
    expect(root.textContent).toContain("Factorial opens the merge requests");
    expect(root.textContent).toContain("GitLab applies these rules");
  });

  it("takes a repository's URL and saves it as owner/name", async () => {
    answer([]);
    const root = await page("repository-setup");
    await type(field(root, "Repository URL"), "https://github.com/northwind/harbor-ledger.git");
    await click(byText(root, "button", "Save repository"));
    await settle(20);
    expect(patches()).toEqual([{ github_repo: "northwind/harbor-ledger" }]);
  });

  it("refuses a repository that is not owner/name before asking the API", async () => {
    answer([]);
    const root = await page("repository-setup");
    await type(field(root, "Repository URL"), "harbor-ledger");
    await click(byText(root, "button", "Save repository"));
    await settle(20);
    expect(root.textContent).toContain("Use a GitHub URL or owner/name.");
    expect(patches()).toEqual([]);
  });

  it("heads each section, with a row nobody could check as information under it", async () => {
    const unread = "not checked: Factorial cannot reach northwind/harbor-invoices";
    answer([OUT_OF_REACH, { ...UNPROTECTED, state: "unknown", summary: unread }]);
    const root = await page("repository-setup");
    await settle(20);
    const headings = [...root.querySelectorAll("section > div:first-child > h3")];
    expect(headings.map((h) => h.textContent)).toEqual(["Repository", "Branch protection"]);
    const state = root.querySelector("[data-branch-checks] h4")?.nextElementSibling;
    expect(state?.textContent).toBe(unread);
    expect(
      state?.querySelector('[data-glyph="status-information"]')?.getAttribute("class"),
    ).toContain("text-icon-information");
  });

  it("says why a row was not checked when the server said", async () => {
    const why = "branch protection not checked: the App lacks administration: read";
    answer([{ ...UNPROTECTED, state: "unknown", summary: "not checked", detail: why }]);
    const root = await page("repository-setup");
    await settle(20);
    const heading = [...root.querySelectorAll("h4")].find(
      (h) => h.textContent === "1. Default branch",
    );
    expect(heading?.nextElementSibling?.textContent).toBe(why);
  });

  it("holds the branch protection, not the labels, and folds the command away", async () => {
    answer([{ ...UNPROTECTED, key: "labels", summary: "2 missing", remedy: null }, UNPROTECTED]);
    const root = await page("repository-setup");
    await settle(20);
    const headings = [...root.querySelectorAll("h3")].map((h) => h.textContent);
    expect(headings).not.toContain("Ticket labels");
    expect(headings).toContain("Branch protection");

    const toggle = byText(root, "button", "Show command");
    expect(toggle?.getAttribute("aria-expanded")).toBe("false");
    const code = document.getElementById(toggle?.getAttribute("aria-controls") ?? "");
    expect(code?.hidden).toBe(true);
    await click(toggle);
    expect(code?.hidden).toBe(false);
    expect(code?.textContent).toContain(UNPROTECTED.remedy);
  });

  it.each(["app-access", "branch-protection", "ci-workflows", "gate-approvers"])(
    "is where the old %s address now goes",
    async (old) => {
      answer([]);
      const root = await page(old);
      expect(root.querySelector("h1")?.textContent).toBe("Git source");
    },
  );
});

describe("Tickets", () => {
  it("holds the ticket source and the labels, the labels' fix in the page's box", async () => {
    answer([{ ...UNPROTECTED, key: "labels", summary: "2 missing", remedy: null }]);
    const root = await page("ticket-source");
    await settle(20);
    expect(root.querySelector("h1")?.textContent).toBe("Tickets");
    const headings = [...root.querySelectorAll("section > div:first-child > h3")];
    expect(headings.map((h) => h.textContent)).toEqual(["Ticket source", "Ticket labels"]);
    const issue = root.querySelector('[data-issue="labels-"]');
    expect(issue?.textContent).toContain("Ticket labels: 2 missing");
    expect(byText(issue as HTMLElement, "button", "Create missing labels")).not.toBeNull();
  });

  it("says why the labels could not be read, with a way to try again", async () => {
    answer([{ ...UNPROTECTED, key: "labels", summary: "2 missing", remedy: null }]);
    const root = await page("ticket-source");
    await settle(20);
    expect(root.textContent).toContain("Couldn't load the labels");
    expect(root.textContent).toContain(UNREACHABLE);
    expect(byText(root, "button", "Try again")).not.toBeNull();
    expect(root.textContent).not.toContain("No process config yet");
  });

  it("says no process is synced yet when the labels read answers that", async () => {
    answer([{ ...UNPROTECTED, key: "labels", summary: "2 missing", remedy: null }]);
    const others = fetchMock.getMockImplementation();
    fetchMock.mockImplementation((url: string, init?: RequestInit) =>
      new URL(String(url), "http://api").pathname.endsWith("/labels")
        ? Promise.resolve(
            new Response(JSON.stringify({ detail: "no process version" }), { status: 409 }),
          )
        : others?.(url, init),
    );
    const root = await page("ticket-source");
    await settle(20);
    expect(root.textContent).toContain("No process config yet");
    expect(root.textContent).not.toContain("Couldn't load the labels");
  });

  it("is where the old labels address now goes", async () => {
    answer([]);
    const root = await page("labels");
    expect(root.querySelector("h1")?.textContent).toBe("Tickets");
  });

  it("offers GitLab, and Jira not yet to be chosen", async () => {
    answer([]);
    const root = await page("ticket-source");
    const source = field(root, "Source") as unknown as HTMLSelectElement | null;
    const options = [...(source?.options ?? [])].map((o) => [o.value, o.disabled]);
    expect(options).toEqual([
      ["github", false],
      ["gitlab", false],
      ["jira", true],
    ]);
  });

  it("sets a GitLab source with its settings, and stores only the secrets typed", async () => {
    answer([]);
    const root = await page("ticket-source");
    await choose(field(root, "Source"), "gitlab");
    await type(field(root, "GitLab URL"), "https://gitlab.example.com/");
    await type(field(root, "GitLab project"), "harbor/finance/invoices");
    await type(field(root, "Access token"), "glpat-token");
    await click(byText(root, "button", "Set source"));
    await settle(20);

    const puts = fetchMock.mock.calls
      .filter(([, init]) => init?.method === "PUT")
      .map(([url, init]) => [
        new URL(String(url), "http://api").pathname.slice(APP_PREFIX.length),
        JSON.parse(String(init?.body)),
      ]);
    expect(puts).toEqual([
      [
        `/api/projects/${PROJECT_ID}/ticket-source`,
        {
          kind: "gitlab",
          external_ref: "harbor/finance/invoices",
          webhook_secret_ref: "GITLAB_WEBHOOK_SECRET",
        },
      ],
      [`/api/projects/${PROJECT_ID}/secrets/GITLAB_TOKEN`, { value: "glpat-token" }],
    ]);
    expect(patches()).toEqual([
      {
        settings: expect.objectContaining({
          forge: "gitlab",
          gitlab: { base_url: "https://gitlab.example.com", bot_login: "", ca_bundle: "" },
        }),
      },
    ]);
  });

  it("refuses a GitLab source with no URL before asking the API", async () => {
    answer([]);
    const root = await page("ticket-source");
    await choose(field(root, "Source"), "gitlab");
    await click(byText(root, "button", "Set source"));
    await settle(20);
    expect(root.textContent).toContain("Enter the GitLab URL, including https://.");
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PUT")).toEqual([]);
  });

  it("sets the source to the project's repository by default", async () => {
    answer([]);
    const root = await page("ticket-source");
    expect(field(root, "Issues repository")?.value).toBe("northwind/harbor-invoices");

    await click(byText(root, "button", "Set source"));
    await settle(20);

    const put = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT");
    expect(new URL(String(put?.[0]), "http://api").pathname.slice(APP_PREFIX.length)).toBe(
      `/api/projects/${PROJECT_ID}/ticket-source`,
    );
    expect(JSON.parse(String(put?.[1]?.body))).toEqual({
      kind: "github",
      external_ref: "northwind/harbor-invoices",
      webhook_secret_ref: null,
    });
  });

  it("has nothing to save until the source changes", async () => {
    answer([]);
    const root = await page("ticket-source", { ticket_source: SOURCE });
    expect(byText(root, "button", "Update source")?.hasAttribute("disabled")).toBe(true);
  });
});

describe("Runtime", () => {
  it("holds the permits, and is where the old permits address now goes", async () => {
    answer([]);
    const root = await page("permits");
    expect(root.querySelector("h1")?.textContent).toBe("Runtime");
    expect(root.textContent).toContain("Egress hosts");
  });
});

describe("a reader who may read the config but not change it", () => {
  const LABELS_MISSING: ReadinessItem = {
    ...UNPROTECTED,
    key: "labels",
    summary: "2 missing",
    remedy: null,
  };
  const PAGES: [string, Partial<ProjectDetail>, string[]][] = [
    ["details", {}, ["Save details"]],
    ["repository-setup", {}, ["Save repository"]],
    ["ticket-source", {}, ["Set source", "Create missing labels"]],
    [
      "runtime",
      { settings: { egress_allowlist: ["registry.npmjs.org"] } },
      ["Add", "Remove registry.npmjs.org"],
    ],
    [
      "secrets",
      { readiness: [SECRETS], declared_secrets: ["DOCS_TOKEN"], secret_names: ["STORED_TOKEN"] },
      ["Save", "Replace value", "Remove", "Save secret"],
    ],
  ];
  const buttons = (root: HTMLElement) =>
    [...root.querySelectorAll("button")].map(
      (button) => button.getAttribute("aria-label") ?? button.textContent?.trim(),
    );
  const writes = () =>
    fetchMock.mock.calls.filter(([, init]) => (init?.method ?? "GET") !== "GET").length;

  it.each(PAGES)(
    "has every write on %s while it holds projects:manage too",
    async (section, over, controls) => {
      answer([LABELS_MISSING]);
      const root = await page(section, over);
      await settle(20);
      expect(buttons(root)).toEqual(expect.arrayContaining(controls));
    },
  );

  it.each(PAGES)("reads %s with no write on it", async (section, over, controls) => {
    answer([LABELS_MISSING]);
    const root = await page(section, over, MEMBER_PERMISSIONS);
    await settle(20);
    expect(root.querySelector("h1")?.textContent).not.toBe("Page not found");
    for (const control of controls) expect(buttons(root)).not.toContain(control);
    const fields = [
      ...root.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>(
        "input:not([type=radio]), textarea",
      ),
    ];
    expect(fields.every((each) => each.readOnly || each.disabled)).toBe(true);
    expect([...root.querySelectorAll("select")].every((each) => each.disabled)).toBe(true);
    expect(writes()).toBe(0);
    // The runtime list needs `runtimes:manage`, which a member lacks.
    expect(paths().some((path) => path.endsWith("/runtimes"))).toBe(false);
  });

  it("still shows what each page holds", async () => {
    answer([]);
    let root = await page("details", { description: "Invoices" }, MEMBER_PERMISSIONS);
    expect(field(root, "Name")?.value).toBe("Harbor invoices");
    expect(field(root, "Description")?.value).toBe("Invoices");
    await view?.unmount();
    root = await page(
      "secrets",
      {
        readiness: [SECRETS],
        secret_names: ["STORED_TOKEN"],
        secret_deliveries: [
          { name: "STORED_TOKEN", mode: "header", host: "api.example.com", header: "x-token" },
        ],
      },
      MEMBER_PERMISSIONS,
    );
    expect(root.querySelector('[data-delivery-for="STORED_TOKEN"]')?.textContent).toBe(
      "Supplied as the header x-token on api.example.com, through the egress proxy.",
    );
    await view?.unmount();
    root = await page(
      "runtime",
      { settings: { runtime_allowlist: ["codex-team"], egress_allowlist: ["registry.npmjs.org"] } },
      MEMBER_PERMISSIONS,
    );
    expect(root.textContent).toContain("codex-team");
    expect(root.textContent).toContain("registry.npmjs.org");
  });

  // Enter in a read-only field still submits its form, so hiding the button is not enough.
  it.each([
    ["holds projects:manage", ADMIN_PERMISSIONS, 1],
    ["holds projects:read alone", MEMBER_PERMISSIONS, 0],
  ] as const)(
    "a submitted details form writes only for a reader who %s",
    async (_, permissions, sent) => {
      answer([]);
      const root = await page("details", {}, permissions);
      await type(field(root, "Description"), "Invoices for the harbor office");
      await act(async () => {
        root
          .querySelector("form")
          ?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      });
      await settle(20);
      expect(patches()).toHaveLength(sent);
    },
  );
});
