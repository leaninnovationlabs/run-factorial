/**
 * The Secrets section's structural invariants: one row per declared key, each saying where its
 * value comes from — minted per run, provided by the process's runtime, or stored on the project
 * — and what, if anything, the operator still has to do about it.
 *
 * Rendered with `renderToString` against a pre-seeded query cache, as `ProcessEditor.test.tsx`
 * is: no fetch, no DOM, and the first paint is the thing under test.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { holding } from "../../api/organization.fixture";
import type { ProjectDetail } from "../../api/types";
import { SecretsBody } from "./SecretsBody";

const PROJECT_ID = 9042;

function project(overrides: Partial<ProjectDetail>): ProjectDetail {
  return {
    id: PROJECT_ID,
    name: "Harbor invoices",
    github_repo: "northwind/harbor-invoices",
    description: null,
    settings: {},
    active: false,
    created_at: "2026-08-01T00:00:00Z",
    ticket_source: null,
    ticket_count: 0,
    open_ticket_count: 0,
    exited_ticket_count: 0,
    secret_names: [],
    declared_secrets: [],
    runtime_provided_secrets: [],
    secret_deliveries: [],
    blockers: [],
    warnings: [],
    readiness: [],
    ...overrides,
  } as unknown as ProjectDetail;
}

function render(detail: ProjectDetail): string {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  holding(qc);
  return renderToString(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SecretsBody project={detail} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const count = (html: string, needle: string) => html.split(needle).length - 1;

describe("the secrets section", () => {
  it("draws one row per declared key, with its provenance", () => {
    const html = render(
      project({
        declared_secrets: ["GH_TOKEN", "OPENAI_API_KEY", "PRODUCT_DOCS_TOKEN"],
        runtime_provided_secrets: [
          {
            name: "OPENAI_API_KEY",
            runtime_slug: "codex-team",
            stored: true,
            open_tickets_reading_copy: 0,
          },
        ],
        secret_names: ["PRODUCT_DOCS_TOKEN"],
      }),
    );
    expect(html).toContain("GH_TOKEN");
    expect(html).toContain("Created automatically for each run");
    expect(html).toContain("Provided by runtime");
    expect(html).toContain('href="/runtimes/codex-team"');
    expect(html).toContain("PRODUCT_DOCS_TOKEN");
    expect(count(html, 'data-secret-state="ok"')).toBe(3);
    expect(html).not.toContain('data-secret-state="missing"');
  });

  it("shows the GitLab token instead of the minted GitHub token when that is the ticket source", () => {
    const html = render(
      project({
        ticket_source: {
          id: 1,
          project_id: PROJECT_ID,
          kind: "gitlab",
          external_ref: "group/app",
          webhook_secret_ref: null,
          last_polled_at: null,
          created_at: "2026-08-01T00:00:00Z",
        },
        declared_secrets: ["GH_TOKEN", "OPENAI_API_KEY"],
        secret_names: ["GITLAB_TOKEN"],
      }),
    );
    expect(html).toContain("GITLAB_TOKEN");
    expect(html).toContain("Stored on this project.");
    // Removing it would cut the project off from GitLab, so it is changed on its own page.
    expect(html).not.toContain('data-remove="GITLAB_TOKEN"');
    expect(html).toContain(`href="/projects/${PROJECT_ID}/configuration/ticket-source"`);
    expect(html).not.toContain("GH_TOKEN");
    expect(html).not.toContain("Created automatically for each run");
  });

  it("marks a runtime that holds no key, and never offers a project form for it", () => {
    const html = render(
      project({
        declared_secrets: ["ANTHROPIC_API_KEY"],
        runtime_provided_secrets: [
          {
            name: "ANTHROPIC_API_KEY",
            runtime_slug: "claude-vm",
            stored: false,
            open_tickets_reading_copy: 0,
          },
        ],
      }),
    );
    expect(html).toContain("The runtime holds no key yet");
    expect(html).toContain('href="/runtimes/claude-vm"');
    expect(count(html, 'data-secret-state="missing"')).toBe(1);
    expect(html).not.toContain('data-store-for="ANTHROPIC_API_KEY"');
  });

  it("offers the store form beneath a project key that is missing, and not beneath one that is stored", () => {
    const html = render(
      project({
        declared_secrets: ["PRODUCT_DOCS_TOKEN", "SLACK_TOKEN"],
        secret_names: ["SLACK_TOKEN"],
      }),
    );
    expect(html).toContain('data-store-for="PRODUCT_DOCS_TOKEN"');
    expect(html).not.toContain('data-store-for="SLACK_TOKEN"');
    expect(count(html, 'data-secret-state="missing"')).toBe(1);
    expect(count(html, 'data-secret-state="ok"')).toBe(1);
  });

  it("says a project-stored copy of a runtime-provided key is superseded, and offers to remove it", () => {
    const html = render(
      project({
        declared_secrets: ["OPENAI_API_KEY"],
        runtime_provided_secrets: [
          {
            name: "OPENAI_API_KEY",
            runtime_slug: "codex-team",
            stored: true,
            open_tickets_reading_copy: 0,
          },
        ],
        secret_names: ["OPENAI_API_KEY"],
      }),
    );
    expect(html).toContain("superseded by runtime");
    expect(html).toContain("No process that names the runtime reads it.");
    expect(html).not.toContain("open ticket");
    expect(html).toContain('data-remove="OPENAI_API_KEY"');
    expect(html).toContain('href="/runtimes/codex-team"');
  });

  it("warns beside Remove when open tickets started before the change still use the copy", () => {
    const html = render(
      project({
        declared_secrets: ["OPENAI_API_KEY"],
        runtime_provided_secrets: [
          {
            name: "OPENAI_API_KEY",
            runtime_slug: "codex-team",
            stored: true,
            open_tickets_reading_copy: 2,
          },
        ],
        secret_names: ["OPENAI_API_KEY"],
      }),
    );
    expect(html).toContain("2 open tickets started before the change still use this copy");
    expect(html).toContain('data-remove="OPENAI_API_KEY"');
  });

  it("lists a stored key nothing declares as project-stored, so it is not invisible", () => {
    const html = render(project({ secret_names: ["LEFTOVER"] }));
    expect(html).toContain("LEFTOVER");
    expect(count(html, 'data-secret-state="ok"')).toBe(1);
  });

  it("says how a cloud sandbox run receives a key routed through the proxy", () => {
    const html = render(
      project({
        declared_secrets: ["LOGS_API_TOKEN"],
        secret_names: ["LOGS_API_TOKEN"],
        secret_deliveries: [
          {
            name: "LOGS_API_TOKEN",
            mode: "header",
            host: "api.example.com",
            header: "project-access-token",
          },
        ],
      }),
    );
    expect(html).toContain("Header");
    expect(html).toContain("project-access-token");
    expect(html).toContain("api.example.com");
    expect(count(html, 'data-secret-state="ok"')).toBe(1);
  });

  it("says a key with no header chosen is supplied as an environment variable", () => {
    const html = render(
      project({
        declared_secrets: ["DB_PASSWORD"],
        secret_names: ["DB_PASSWORD"],
        secret_deliveries: [{ name: "DB_PASSWORD", mode: "env", host: null, header: null }],
      }),
    );
    expect(html).toContain("Environment variable");
    expect(html).not.toContain('data-secret-state="warn"');
  });

  it("keeps a form for a name the manifests do not declare yet", () => {
    const html = render(project({}));
    expect(html).toContain("Save secret");
  });
});

describe("the connection secrets", () => {
  it("shows the GitHub connection's stored secrets as managed, with no store form", () => {
    const html = render(
      project({
        secret_names: ["GITHUB_CONNECTION_TOKEN"],
      }),
    );
    expect(html).toContain("GITHUB_CONNECTION_TOKEN");
    expect(html).toContain("Managed with the GitHub connection");
    expect(html).not.toContain('data-store-for="GITHUB_CONNECTION_TOKEN"');
  });
});
