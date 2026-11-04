/**
 * The Projects list's structural invariants, rendered with `renderToString` against a
 * pre-seeded query cache as `RuntimesPage.test.tsx` is: no fetch, no DOM, first paint only.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { holding, MEMBER_PERMISSIONS } from "../api/organization.fixture";
import { keys } from "../api/queries";
import type { ProjectListItem } from "../api/types";
import { ProjectsPage } from "./ProjectsPage";

function project(id: number, runtime: ProjectListItem["runtime"], active = true): ProjectListItem {
  return {
    id,
    name: `project ${id}`,
    github_repo: `acme/repo-${id}`,
    description: null,
    settings: {},
    active,
    created_at: "2026-09-01T09:00:00Z",
    runtime,
  };
}

const PROJECTS: ProjectListItem[] = [
  project(1, { slug: "codex-team", name: "Codex team" }),
  project(2, null),
  project(3, { slug: "vanished", name: null }),
];

function render(
  rows: ProjectListItem[] = PROJECTS,
  url = "/projects",
  permissions?: string[],
): string {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  holding(qc, permissions);
  qc.setQueryData(keys.projects, rows);
  return renderToString(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route path="/projects" element={<ProjectsPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const columns = (html: string) =>
  [...html.matchAll(/<th[^>]*>([^<]*)<\/th>/g)].map((match) => match[1]);

describe("the list", () => {
  it("heads its columns Name, Repository, Runtime, Admission, Created — in that order", () => {
    expect(columns(render())).toEqual(["Name", "Repository", "Runtime", "Admission", "Created"]);
  });

  it("opens a GitLab project's repository on GitLab", () => {
    const html = render([
      {
        ...project(1, null),
        github_repo: "group/project",
        settings: { gitlab: { base_url: "https://gitlab.com" } },
        ticket_source: {
          id: 1,
          project_id: 1,
          kind: "gitlab",
          external_ref: "group/project",
          webhook_secret_ref: null,
          last_polled_at: null,
          created_at: "2026-09-01T09:00:00Z",
        },
      },
    ]);
    expect(html).toContain('href="https://gitlab.com/group/project"');
    expect(html).not.toContain("github.com");
  });

  it("names the runtime the process binds and links it to its page", () => {
    const html = render();
    expect(html).toContain("Codex team");
    expect(html).toContain('href="/runtimes/codex-team"');
    expect(html).toContain("acme/repo-1");
  });

  it("offers a member no way to create a project or open a runtime", () => {
    const html = render(PROJECTS, "/projects", MEMBER_PERMISSIONS);
    expect(html).toContain("Codex team");
    expect(html).not.toContain('href="/runtimes/');
    expect(html).not.toContain("Create project");
  });

  it("says None for a project whose process names no runtime yet", () => {
    const html = render([project(2, null)]);
    expect(html).toContain("None");
    expect(html).not.toContain('href="/runtimes/');
  });

  it("shows the slug, unlinked, when the process names a runtime that no longer exists", () => {
    const html = render([project(3, { slug: "vanished", name: null })]);
    expect(html).toContain("vanished");
    expect(html).not.toContain('href="/runtimes/vanished"');
  });
});

describe("the admission filter", () => {
  const MIXED = [project(1, null, true), project(2, null, false)];

  it("hides paused projects by default", () => {
    const html = render(MIXED);
    expect(html).toContain("project 1");
    expect(html).not.toContain("project 2");
  });

  it("shows every project under ?show=all", () => {
    const html = render(MIXED, "/projects?show=all");
    expect(html).toContain("project 1");
    expect(html).toContain("project 2");
  });

  it("shows only paused projects under ?show=paused", () => {
    const html = render(MIXED, "/projects?show=paused");
    expect(html).not.toContain("project 1");
    expect(html).toContain("project 2");
  });

  it("still says there are no projects yet when there are none", () => {
    expect(render([])).toContain("No projects yet");
  });
});
