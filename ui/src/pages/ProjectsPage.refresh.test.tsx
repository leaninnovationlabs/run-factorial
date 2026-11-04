// @vitest-environment happy-dom
/**
 * The project list's refresh, clicked for real: it wakes the worker and refetches the list, and
 * keeps its spinner for a second unless the list changes sooner.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../api/client";
import { keys, REFRESH_MIN_MS } from "../api/queries";
import type { ProjectListItem } from "../api/types";
import { byText, click, type Mounted, mount, settle } from "../testDom";
import { ProjectsPage } from "./ProjectsPage";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.useRealTimers();
  vi.restoreAllMocks();
});

const PROJECT: ProjectListItem = {
  id: 1,
  name: "project 1",
  github_repo: "acme/repo-1",
  description: null,
  settings: {},
  active: true,
  created_at: "2026-09-01T09:00:00Z",
  runtime: null,
};

const wait = (ms: number) => act(async () => void (await vi.advanceTimersByTimeAsync(ms)));
const busy = (root: HTMLElement) =>
  byText(root, "button", "Refresh")?.getAttribute("aria-busy") === "true";

async function page() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Number.POSITIVE_INFINITY } },
  });
  qc.setQueryData(keys.projects, []);
  view = await mount(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/projects"]}>
        <Routes>
          <Route path="/projects" element={<ProjectsPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return view.container;
}

const texts = (root: HTMLElement, role: string) =>
  [...root.querySelectorAll(`[role="${role}"]`)].map((node) => node.textContent ?? "");

describe("the projects page's refresh", () => {
  it("spins for a second when nothing changes", async () => {
    vi.useFakeTimers();
    const wake = vi
      .spyOn(api, "refreshFleet")
      .mockResolvedValue({ requested_at: "2026-09-23T09:00:00Z" });
    const list = vi.spyOn(api, "listProjects").mockResolvedValue([]);
    const root = await page();
    await click(byText(root, "button", "Refresh"));
    await wait(REFRESH_MIN_MS - 200);
    expect(wake).toHaveBeenCalledOnce();
    expect(list).toHaveBeenCalled();
    expect(busy(root)).toBe(true);
    await wait(400);
    expect(busy(root)).toBe(false);
  });

  it("stops spinning as soon as the list changes", async () => {
    vi.useFakeTimers();
    vi.spyOn(api, "refreshFleet").mockResolvedValue({ requested_at: "2026-09-23T09:00:00Z" });
    vi.spyOn(api, "listProjects").mockResolvedValue([PROJECT]);
    const root = await page();
    await click(byText(root, "button", "Refresh"));
    await wait(50);
    expect(busy(root)).toBe(false);
    expect(root.textContent).toContain("project 1");
  });

  it("says why a refresh failed, as an error", async () => {
    vi.spyOn(api, "refreshFleet").mockRejectedValue(new ApiError(503, "database unavailable"));
    const root = await page();
    await click(byText(root, "button", "Refresh"));
    await settle();
    expect(texts(root, "alert")).toContain("database unavailable");
  });
});
