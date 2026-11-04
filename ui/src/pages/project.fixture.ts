import type { ProjectDetail, ReadinessItem } from "../api/types";

export const PROJECT_ID = 9041;

export function project(over: Partial<ProjectDetail> = {}): ProjectDetail {
  return {
    id: PROJECT_ID,
    name: "Harbor invoices",
    github_repo: "northwind/harbor-invoices",
    description: null,
    settings: {},
    active: true,
    created_at: "2026-07-02T14:18:03Z",
    ticket_source: null,
    processes: [],
    default_process: null,
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
    ...over,
  };
}

/** The rows the project's own payload always carries, every one ready. */
export function readyRows(): ReadinessItem[] {
  const row = (key: ReadinessItem["key"], summary: string): ReadinessItem => ({
    key,
    process: null,
    runtime_slug: null,
    state: "ok",
    summary,
    detail: null,
    remedy: null,
    link: null,
  });
  return [
    row("ticket_source", "northwind/harbor-invoices"),
    row("process_config", "simple at ab12cd3"),
    row("secrets", "every declared secret is stored"),
  ];
}
