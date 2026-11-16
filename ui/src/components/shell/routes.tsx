/**
 * Every page, with how wide it reads and whether it fills the window (`AppShell` lays them out).
 *
 * `full` is for the pages that spend width on information — a board, tables, a graph, a log;
 * `default` for a page of panels; `narrow` for a form, where a stretched line reads worse. A
 * `fill` page is exactly the height of the window below the top bar, and its own regions scroll
 * inside it: the board and its list, the ticket's activity. A `bleed` page takes the window to
 * its edges on a phone, where the page's own frame would only spend width: the assistant. A
 * `focus` page is drawn without the side navigation, as `useFocusMode` puts it away for a while:
 * creating an organization, which is about none of the current one's places. Its Cancel and the
 * top bar's mark are the way back.
 *
 * A page with `needs` is drawn only for a reader who holds that permission (`RequirePermission`):
 * the settings pages, whose reads the API keeps to the roles that hold them. A project's
 * configuration needs only `projects:read`; its pages leave out what writes for a reader without
 * `projects:manage`.
 *
 * Each page is its own chunk, loaded when it is first visited — or a moment earlier, when the
 * pointer or the focus reaches a link to it (`preloadRoute`).
 */

import { type ComponentType, type LazyExoticComponent, lazy } from "react";
import { matchPath } from "react-router-dom";

export type RouteWidth = "full" | "default" | "narrow";

type Loader = () => Promise<{ default: ComponentType }>;

export type AppRoute = {
  path: string;
  page: { Component: LazyExoticComponent<ComponentType>; load: Loader };
  width: RouteWidth;
  fill?: boolean;
  bleed?: boolean;
  focus?: boolean;
  needs?: string;
};

/** One loader per page, shared by the lazy component and the preload, so both ask for one chunk. */
function page(load: Loader): AppRoute["page"] {
  return { Component: lazy(load), load };
}

const PAGES = {
  overview: page(() =>
    import("../../pages/OverviewPage").then((m) => ({ default: m.OverviewPage })),
  ),
  projects: page(() =>
    import("../../pages/ProjectsPage").then((m) => ({ default: m.ProjectsPage })),
  ),
  newProject: page(() =>
    import("../../pages/NewProjectPage").then((m) => ({ default: m.NewProjectPage })),
  ),
  projectDashboard: page(() =>
    import("../../pages/ProjectDashboardPage").then((m) => ({ default: m.ProjectDashboardPage })),
  ),
  projectTickets: page(() =>
    import("../../pages/ProjectTicketsPage").then((m) => ({ default: m.ProjectTicketsPage })),
  ),
  projectConfiguration: page(() =>
    import("../../pages/ProjectConfigurationPage").then((m) => ({
      default: m.ProjectConfigurationPage,
    })),
  ),
  projectConfigurationSection: page(() =>
    import("../../pages/ProjectConfigurationSectionPage").then((m) => ({
      default: m.ProjectConfigurationSectionPage,
    })),
  ),
  ticket: page(() => import("../../pages/TicketPage").then((m) => ({ default: m.TicketPage }))),
  runtimes: page(() =>
    import("../../pages/RuntimesPage").then((m) => ({ default: m.RuntimesPage })),
  ),
  newRuntime: page(() =>
    import("../../pages/NewRuntimePage").then((m) => ({ default: m.NewRuntimePage })),
  ),
  runtime: page(() => import("../../pages/RuntimePage").then((m) => ({ default: m.RuntimePage }))),
  assistant: page(() =>
    import("../../pages/AssistantPage").then((m) => ({ default: m.AssistantPage })),
  ),
  mcpAccess: page(() => import("../../pages/McpAccessPage")),
  settings: page(() => import("../../pages/SettingsPage")),
  members: page(() => import("../../pages/MembersPage")),
  inviteMembers: page(() => import("../../pages/InviteMembersPage")),
  newOrganization: page(() => import("../../pages/NewOrganizationPage")),
};

export const ROUTES: AppRoute[] = [
  { path: "/overview", page: PAGES.overview, width: "full", fill: true },
  { path: "/projects", page: PAGES.projects, width: "full" },
  { path: "/projects/new", page: PAGES.newProject, width: "narrow", needs: "projects:manage" },
  // A project is a place with pages (tech-stack decision 69): its dashboard is its home.
  { path: "/projects/:projectId", page: PAGES.projectDashboard, width: "full" },
  { path: "/projects/:projectId/tickets", page: PAGES.projectTickets, width: "full" },
  {
    path: "/projects/:projectId/configuration",
    page: PAGES.projectConfiguration,
    width: "full",
    needs: "projects:read",
  },
  {
    path: "/projects/:projectId/configuration/:section",
    page: PAGES.projectConfigurationSection,
    width: "full",
    needs: "projects:read",
  },
  { path: "/tickets/:ticketId", page: PAGES.ticket, width: "full", fill: true },
  { path: "/runtimes", page: PAGES.runtimes, width: "full", needs: "runtimes:manage" },
  { path: "/runtimes/new", page: PAGES.newRuntime, width: "default", needs: "runtimes:manage" },
  { path: "/runtimes/:slug", page: PAGES.runtime, width: "default", needs: "runtimes:manage" },
  {
    path: "/assistant",
    page: PAGES.assistant,
    width: "full",
    fill: true,
    bleed: true,
    needs: "assistant:use",
  },
  { path: "/account/mcp", page: PAGES.mcpAccess, width: "full", needs: "mcp_tokens:mint" },
  { path: "/settings", page: PAGES.settings, width: "full", needs: "assistant:configure" },
  { path: "/members", page: PAGES.members, width: "full", needs: "members:invite" },
  {
    path: "/members/invite",
    page: PAGES.inviteMembers,
    width: "narrow",
    needs: "members:invite",
  },
  // No `needs`: making another organization is not a permission held in this one.
  { path: "/organizations/new", page: PAGES.newOrganization, width: "narrow", focus: true },
];

export const WIDTH_CLASS: Record<RouteWidth, string> = {
  full: "",
  default: "max-w-page-default",
  narrow: "max-w-page-narrow",
};

/** The route a path lands on, or null for one the app does not have. */
export function routeFor(pathname: string): AppRoute | null {
  return ROUTES.find((route) => matchPath(route.path, pathname) !== null) ?? null;
}

/** Start loading the page a link goes to, so following it does not wait on the network. A failed
 *  preload is dropped: the visit will ask again, and say so if it fails then. */
export function preloadRoute(pathname: string): void {
  routeFor(pathname)
    ?.page.load()
    .catch(() => {});
}
