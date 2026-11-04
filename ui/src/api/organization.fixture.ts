import type { QueryClient } from "@tanstack/react-query";
import { keys } from "./keys";
import type { OrganizationDetail } from "./types";

/** What the built-in roles hold after revision 0037, with 0038's `projects:read`; the API's
 *  `GET /api/roles` is the source. */
export const MEMBER_PERMISSIONS = [
  "org:read",
  "projects:read",
  "tickets:operate",
  "assistant:use",
  "mcp_tokens:mint",
];
export const ADMIN_PERMISSIONS = [
  ...MEMBER_PERMISSIONS,
  "org:manage",
  "members:invite",
  "members:remove",
  "projects:manage",
  "runtimes:manage",
  "assistant:configure",
  "tickets:terminal",
  "mcp_tokens:manage",
];

export function organizationDetail(permissions: string[] = ADMIN_PERMISSIONS): OrganizationDetail {
  return {
    slug: "default",
    name: "Northwind",
    project_count: 1,
    runtime_count: 0,
    updated_by: "operator@example.com",
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    role: null,
    settings: permissions.includes("org:manage") ? {} : null,
    permissions,
  };
}

/** The page's organization, read as a reader who holds `permissions`. Never stale, so a mounted
 *  page does not ask the API for it again over the seed. */
export function holding(qc: QueryClient, permissions: string[] = ADMIN_PERMISSIONS): void {
  qc.setQueryDefaults(keys.organization, { staleTime: Number.POSITIVE_INFINITY });
  qc.setQueryData(keys.organization, organizationDetail(permissions));
}
