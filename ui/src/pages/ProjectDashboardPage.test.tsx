// @vitest-environment happy-dom
/**
 * The project's home, and what polls behind it. The Tickets and Admission reads — the second of
 * which makes live GitHub calls — must not poll behind a page that does not draw them; the
 * dashboard read itself carries no interval, so a page left open costs nothing, but follows the
 * page's Refresh. A project with a blocker says so and points at the page that clears it.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { holding, MEMBER_PERMISSIONS } from "../api/organization.fixture";
import { keys, REFRESH_MIN_MS } from "../api/queries";
import { seedFailure } from "../api/queryTesting";
import type { ProjectDetail } from "../api/types";
import { dashboard } from "../components/dashboard/dashboard.fixture";
import { APP_PREFIX } from "../prefix";
import { byText, click, type Mounted, mount, settle } from "../testDom";
import { ProjectDashboardPage } from "./ProjectDashboardPage";
import { PROJECT_ID, project } from "./project.fixture";

const fetchMock = vi.fn();
let view: Mounted | null = null;
let qc: QueryClient;

beforeEach(() => {
  fetchMock.mockImplementation(() => Promise.reject(new TypeError("offline")));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(async () => {
  await view?.unmount();
  view = null;
  fetchMock.mockReset();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

async function page(
  seed: (qc: QueryClient) => void = (qc) => qc.setQueryData(keys.project(PROJECT_ID), project()),
  search = "",
): Promise<HTMLElement> {
  qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false, retryOnMount: false, staleTime: Number.POSITIVE_INFINITY },
    },
  });
  holding(qc);
  seed(qc);
  view = await mount(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/projects/${PROJECT_ID}${search}`]}>
        <Routes>
          <Route path="/projects/:projectId" element={<ProjectDashboardPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await settle();
  return view.container;
}

const withProject = (over: Partial<ProjectDetail>) => (qc: QueryClient) =>
  qc.setQueryData(keys.project(PROJECT_ID), project(over));

/** The requests the page made, as URLs; `fetch` is called with `${BASE_URL}${path}`. */
const requested = () => fetchMock.mock.calls.map(([url]) => new URL(String(url), "http://api"));
const paths = () => requested().map((url) => url.pathname.slice(APP_PREFIX.length));

describe("the project's dashboard page", () => {
  it("keeps the project on screen when a refetch fails, and says it is stale", async () => {
    const root = await page((qc) => {
      seedFailure(qc, keys.project(PROJECT_ID), "cannot reach the API", project());
      qc.setQueryData(keys.dashboard(PROJECT_ID, "all"), dashboard());
    });
    expect(root.textContent).toContain("Harbor invoices");
    expect(root.textContent).toContain("Couldn't refresh this page. Showing data from");
    expect(root.textContent).not.toContain("cannot reach the API");
  });

  it("shows the error in place of the page only when nothing has loaded yet", async () => {
    const root = await page((qc) =>
      seedFailure(qc, keys.project(PROJECT_ID), "cannot reach the API"),
    );
    expect(root.textContent).toContain("cannot reach the API");
    expect(root.textContent).not.toContain("Harbor invoices");
  });

  it("reads the dashboard, and polls neither the tickets nor the open issues", async () => {
    await page();
    expect(paths()).toContain(`/api/projects/${PROJECT_ID}/dashboard`);
    expect(paths()).not.toContain(`/api/projects/${PROJECT_ID}/tickets`);
    expect(paths()).not.toContain(`/api/projects/${PROJECT_ID}/pending`);
    expect(paths()).not.toContain(`/api/projects/${PROJECT_ID}/readiness`);
  });

  it("points a project with a blocker at the page of its first unfinished requirement", async () => {
    const root = await page(
      withProject({
        active: false,
        blockers: ["Create the missing labels."],
        readiness: [
          {
            key: "labels",
            process: null,
            runtime_slug: null,
            state: "blocked",
            summary: "2 labels are missing",
            detail: null,
            remedy: null,
            link: null,
          },
        ],
      }),
    );
    expect(root.textContent).toContain("Finish configuration to activate");
    expect(byText(root, "a", "View project settings")?.getAttribute("href")).toBe(
      // The labels live on the Tickets settings page, with the tracker.
      `/projects/${PROJECT_ID}/configuration/ticket-source`,
    );
    expect(byText(root, "button", "View pending configuration step")).not.toBeNull();
  });

  it("tells a member why nothing is admitted, with the way into the settings and no admission", async () => {
    const root = await page((qc) => {
      withProject({ active: false, blockers: ["Create the missing labels."] })(qc);
      holding(qc, MEMBER_PERMISSIONS);
    });
    expect(root.textContent).toContain("Create the missing labels.");
    expect(byText(root, "a", "View project settings")).toBeTruthy();
    expect(byText(root, "button", "View pending configuration step")).toBeFalsy();
    expect(byText(root, "button", "Activate")).toBeFalsy();
  });

  it("asks the API for all time, even from an old link that names a shorter window", async () => {
    await page(undefined, "?span=4w");
    const read = requested().find((url) => url.pathname.endsWith("/dashboard"));
    expect(read?.searchParams.get("span")).toBe("all");
  });

  it("gives the dashboard read no interval", async () => {
    await page();
    const query = qc
      .getQueryCache()
      .find({ queryKey: keys.dashboard(PROJECT_ID, "all"), exact: true });
    expect(query?.observers).toHaveLength(1);
    expect(query?.observers[0]?.options.refetchInterval).toBeUndefined();
  });
});

describe("the project page's refresh", () => {
  it("waits out the worker's pass, then refetches the dashboard", async () => {
    vi.spyOn(api, "refreshFleet").mockResolvedValue({ requested_at: "2026-10-09T15:37:00Z" });
    // Every answer is a new body, as the API's `as_of` makes it.
    let minute = 0;
    const read = vi
      .spyOn(api, "getDashboard")
      .mockImplementation(async () =>
        dashboard({ as_of: `2026-10-09T15:${String(minute++).padStart(2, "0")}:00Z` }),
      );
    const root = await page();
    vi.useFakeTimers();
    const wait = (ms: number) => act(async () => void (await vi.advanceTimersByTimeAsync(ms)));
    const busy = () => byText(root, "button", "Refresh")?.getAttribute("aria-busy") === "true";
    const before = read.mock.calls.length;

    await click(byText(root, "button", "Refresh"));
    await wait(REFRESH_MIN_MS - 200);
    expect(busy()).toBe(true);
    await wait(400);
    expect(busy()).toBe(false);
    expect(read.mock.calls.length).toBe(before + 1);
  });
});
