// @vitest-environment happy-dom
/**
 * The Projects list's All / Active / Paused, pressed: All is the default, and the reader's last
 * press is kept, so coming back to the list finds it as they left it. A `show` in a followed link
 * applies but is not kept.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { holding } from "../api/organization.fixture";
import { keys } from "../api/queries";
import type { ProjectListItem } from "../api/types";
import { click, type Mounted, mount } from "../testDom";
import { PROJECTS_SHOW_KEY, ProjectsPage } from "./ProjectsPage";

let view: Mounted | null = null;

beforeEach(() => window.localStorage.clear());

afterEach(async () => {
  await view?.unmount();
  view = null;
  window.localStorage.clear();
});

const project = (id: number, active: boolean): ProjectListItem => ({
  id,
  name: active ? `live ${id}` : `paused ${id}`,
  github_repo: `acme/repo-${id}`,
  description: null,
  settings: {},
  active,
  created_at: "2026-09-01T09:00:00Z",
  runtime: null,
});

function Where() {
  return <output data-where>{useLocation().search}</output>;
}

/** The list, opened afresh at `url`, as a reader navigating back to it would open it. */
async function open(url = "/projects") {
  await view?.unmount();
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Number.POSITIVE_INFINITY } },
  });
  holding(qc);
  qc.setQueryData(keys.projects, [project(1, true), project(2, false)]);
  view = await mount(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}>
        <Where />
        <Routes>
          <Route path="/projects" element={<ProjectsPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return view.container;
}

/** An option of the control, whose text is its label and then its count. */
const option = (root: HTMLElement, label: string) =>
  [...root.querySelectorAll<HTMLElement>('[role="group"] button')].find((button) =>
    button.textContent?.startsWith(label),
  );
const pressed = (root: HTMLElement) =>
  root.querySelector('[role="group"] button[aria-pressed="true"]')?.textContent ?? "";
const listed = (root: HTMLElement) => ({
  live: root.textContent?.includes("live 1"),
  paused: root.textContent?.includes("paused 2"),
});

describe("the Projects list's admission filter", () => {
  it("opens on All, paused projects listed too", async () => {
    const root = await open();
    expect(pressed(root)).toMatch(/^All/);
    expect(listed(root)).toEqual({ live: true, paused: true });
  });

  it("keeps the last press for the next visit, and All again once All is pressed", async () => {
    let root = await open();
    await click(option(root, "Paused"));
    expect(listed(root)).toEqual({ live: false, paused: true });

    root = await open();
    expect(pressed(root)).toMatch(/^Paused/);
    expect(listed(root)).toEqual({ live: false, paused: true });

    await click(option(root, "Active"));
    root = await open();
    expect(pressed(root)).toMatch(/^Active/);
    expect(listed(root)).toEqual({ live: true, paused: false });

    await click(option(root, "All"));
    expect(root.querySelector("[data-where]")?.textContent).toBe("");
    root = await open();
    expect(pressed(root)).toMatch(/^All/);
  });

  it("follows a link's show without keeping it", async () => {
    window.localStorage.setItem(PROJECTS_SHOW_KEY, "active");
    let root = await open("/projects?show=paused");
    expect(listed(root)).toEqual({ live: false, paused: true });
    root = await open();
    expect(pressed(root)).toMatch(/^Active/);
  });
});
