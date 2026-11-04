// @vitest-environment happy-dom
/**
 * The Project settings overview, and when the repo-side probes run: on arrival, so the live rows
 * answer without the reader going through a section's page first, and again on Check repository.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { holding, MEMBER_PERMISSIONS } from "../api/organization.fixture";
import { keys } from "../api/queries";
import type { ProjectDetail, ProjectReadiness } from "../api/types";
import { UNCHECKED } from "../components/configuration/readiness";
import { byText, click, type Mounted, mount, settle } from "../testDom";
import { ProjectConfigurationPage } from "./ProjectConfigurationPage";
import { PROJECT_ID, project, readyRows } from "./project.fixture";

const PROBED: ProjectReadiness = {
  readiness: [
    {
      key: "app_access",
      process: null,
      runtime_slug: null,
      state: "ok",
      summary: "installed on northwind",
      detail: null,
      remedy: null,
      link: null,
    },
    {
      key: "labels",
      process: null,
      runtime_slug: null,
      state: "warn",
      summary: "2 labels are missing",
      detail: null,
      remedy: null,
      link: null,
    },
  ],
  warnings: [],
};

const fetchMock = vi.fn();
let view: Mounted | null = null;

beforeEach(() => {
  fetchMock.mockImplementation((url: string) => {
    if (String(url).endsWith(`/api/projects/${PROJECT_ID}/readiness`)) {
      return Promise.resolve(
        new Response(JSON.stringify(PROBED), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    }
    return Promise.reject(new TypeError("offline"));
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(async () => {
  await view?.unmount();
  view = null;
  fetchMock.mockReset();
  vi.unstubAllGlobals();
});

async function page(
  over: Partial<ProjectDetail> = {},
  seed: (qc: QueryClient) => void = () => {},
): Promise<HTMLElement> {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Number.POSITIVE_INFINITY } },
  });
  qc.setQueryData(keys.project(PROJECT_ID), project({ readiness: readyRows(), ...over }));
  seed(qc);
  view = await mount(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/projects/${PROJECT_ID}/configuration`]}>
        <Routes>
          <Route path="/projects/:projectId/configuration" element={<ProjectConfigurationPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await settle();
  return view.container;
}

const paths = () =>
  fetchMock.mock.calls.map(([url]) => new URL(String(url), "http://api").pathname);
const readinessReads = () => paths().filter((path) => path.endsWith("/readiness")).length;

describe("the Project settings overview", () => {
  it("runs the repo-side probes when opened, and draws what they found", async () => {
    const root = await page();
    await settle(20);
    expect(readinessReads()).toBe(1);
    expect(root.querySelector('[data-ledger-row="ticket-source"]')?.textContent).toContain(
      "2 labels are missing",
    );
    expect(root.textContent).not.toContain(UNCHECKED);
    expect(root.textContent).toContain("Repository checked");
  });

  it("runs them again on Check repository", async () => {
    const root = await page();
    await settle(20);
    await click(byText(root, "button", "Check repository"));
    await settle(20);
    expect(readinessReads()).toBe(2);
  });

  it("shows the last answer while the new one is on its way", async () => {
    const root = await page({}, (qc) => {
      qc.setQueryDefaults(keys.projectReadiness(PROJECT_ID), { staleTime: 0 });
      qc.setQueryData(keys.projectReadiness(PROJECT_ID), PROBED);
      fetchMock.mockImplementation(() => new Promise(() => {}));
    });
    expect(readinessReads()).toBe(1);
    expect(root.querySelector('[data-ledger-row="repository-setup"]')?.textContent).toContain(
      "installed on northwind",
    );
  });

  it("is a list of ways in: one row per configuration page", async () => {
    const root = await page();
    const rows = [...root.querySelectorAll<HTMLAnchorElement>("[data-ledger-row]")];
    expect(rows.map((row) => row.getAttribute("href"))).toEqual([
      `/projects/${PROJECT_ID}/configuration/details`,
      `/projects/${PROJECT_ID}/configuration/repository-setup`,
      `/projects/${PROJECT_ID}/configuration/process`,
      `/projects/${PROJECT_ID}/configuration/ticket-source`,
      `/projects/${PROJECT_ID}/configuration/runtime`,
      `/projects/${PROJECT_ID}/configuration/secrets`,
    ]);
  });

  it("lists what stops an activation, and sends the reader to the first of it", async () => {
    const root = await page({
      active: false,
      blockers: ["Set the ticket source."],
      readiness: [
        {
          key: "ticket_source",
          process: null,
          runtime_slug: null,
          state: "blocked",
          summary: "No ticket source",
          detail: null,
          remedy: null,
          link: null,
        },
      ],
    });
    expect(root.textContent).toContain("Finish configuration to activate");
    expect(byText(root, "a", "Go to the first requirement")?.getAttribute("href")).toBe(
      `/projects/${PROJECT_ID}/configuration/ticket-source`,
    );
    expect(byText(root, "a", "Go to the first requirement")?.getAttribute("data-appearance")).toBe(
      "default",
    );
  });

  it("gives a reader without projects:manage the ledger and Check repository, and no admission", async () => {
    const root = await page({ active: false }, (qc) => holding(qc, MEMBER_PERMISSIONS));
    await settle(20);
    expect(root.querySelectorAll("[data-ledger-row]")).toHaveLength(6);
    expect(byText(root, "button", "Check repository")).not.toBeNull();
    expect(byText(root, "button", "Activate project")).toBeNull();
    expect(paths().some((path) => path.endsWith("/runtimes"))).toBe(false);
  });

  it("offers admission to a reader who holds projects:manage", async () => {
    const root = await page({ active: false }, (qc) => holding(qc));
    expect(byText(root, "button", "Activate project")).not.toBeNull();
  });
});
