/**
 * A project's pages, and where in a project an address is.
 *
 * A project is a place with pages, not a page with tabs: its dashboard at `/projects/:id`, its
 * tickets, its configuration overview and one page per configuration section (tech-stack decision
 * 69). Every link into one is written here, so a page that moves moves once. A ticket's page stays
 * at `/tickets/:ticketId` — comments on GitHub, sign-in return paths and the assistant's cards all
 * address it there — and the navigation learns its project from the ticket itself
 * (`useProjectPlace`).
 */

import { matchPath } from "react-router-dom";

export type ProjectArea = "dashboard" | "tickets" | "configuration";

export type ProjectPlace = {
  projectId: number;
  area: ProjectArea;
  /** The configuration section's slug on a section page; null everywhere else. */
  section: string | null;
};

export const projectHref = (projectId: number) => `/projects/${projectId}`;
export const projectTicketsHref = (projectId: number) => `/projects/${projectId}/tickets`;
export const configurationHref = (projectId: number, section?: string | null) =>
  section
    ? `/projects/${projectId}/configuration/${section}`
    : `/projects/${projectId}/configuration`;

/** Where an address from before a project had pages now goes: `/projects/:id?tab=` named the
 *  page, and Show me cards stored then still do. `section` is the configuration page to open, if
 *  the old address named the editor. Null for an address that names no other page. */
export function legacyTabHref(
  pathname: string,
  tab: unknown,
  section: string | null = null,
): string | null {
  const match = matchPath("/projects/:projectId", pathname);
  const projectId = Number(match?.params.projectId);
  if (!Number.isInteger(projectId) || projectId <= 0) return null;
  if (tab === "tickets") return projectTicketsHref(projectId);
  if (tab === "configuration") return configurationHref(projectId, section);
  return null;
}

/** The same page of another project — the tickets list, the configuration overview, the
 *  dashboard. Never a section: the other project may not have it. */
export function sameAreaHref(projectId: number, area: ProjectArea): string {
  if (area === "tickets") return projectTicketsHref(projectId);
  if (area === "configuration") return configurationHref(projectId);
  return projectHref(projectId);
}

export type Place =
  | { kind: "project"; place: ProjectPlace }
  | { kind: "ticket"; ticketId: number }
  | null;

/** Which project's page, or which ticket's, `pathname` is. Null outside both. A project id that
 *  is not a positive integer is a page too — one that says the project is not found. */
export function placeOf(pathname: string): Place {
  const ticket = matchPath("/tickets/:ticketId", pathname);
  if (ticket) {
    const ticketId = Number(ticket.params.ticketId);
    return Number.isInteger(ticketId) && ticketId > 0 ? { kind: "ticket", ticketId } : null;
  }
  const section = matchPath("/projects/:projectId/configuration/:section", pathname);
  const configuration = section ?? matchPath("/projects/:projectId/configuration", pathname);
  const tickets = matchPath("/projects/:projectId/tickets", pathname);
  const dashboard = matchPath("/projects/:projectId", pathname);
  const match = configuration ?? tickets ?? dashboard;
  if (!match || match.params.projectId === "new") return null;
  const projectId = Number(match.params.projectId);
  return {
    kind: "project",
    place: {
      projectId,
      area: configuration ? "configuration" : tickets ? "tickets" : "dashboard",
      section: section?.params.section ?? null,
    },
  };
}
