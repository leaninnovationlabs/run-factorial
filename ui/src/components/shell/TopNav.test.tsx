// @vitest-environment happy-dom
/**
 * The top bar says where the reader is and switches it: after the mark, the organization and,
 * inside a project, the project, each a link to its home beside the chevron that opens its menu. Below 1024px the search folds to an
 * icon so the names keep the bar; on a phone the bar names the place as labels, and the
 * navigation the tab bar's Menu opens holds the menus.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { holding } from "../../api/organization.fixture";
import { keys } from "../../api/queries";
import type { Organization, ProjectListItem, TicketDetail } from "../../api/types";
import { PROJECT_ID, project } from "../../pages/project.fixture";
import { click, hover, type Mounted, mount, settle } from "../../testDom";
import { AssistantProvider } from "../assistant/AssistantProvider";
import { BELOW_LG, PHONE } from "../ui/media";
import { TopNav } from "./TopNav";

let view: Mounted | null = null;

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.reject(new TypeError("offline"))),
  );
});

afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.unstubAllGlobals();
});

const org = (slug: string, name: string, projects: number): Organization => ({
  slug,
  name,
  project_count: projects,
  runtime_count: 0,
  updated_by: "operator@example.com",
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-01T00:00:00Z",
  role: null,
});

const OTHER: ProjectListItem = {
  ...project(),
  id: 2,
  name: "Anchor ledgers",
  github_repo: "northwind/anchor-ledgers",
  runtime: null,
};

/** The window's width, as the media queries a browser of that width would answer. */
function width(size: "wide" | "medium" | "phone") {
  const matching =
    size === "phone" ? [PHONE, BELOW_LG] : size === "medium" ? [BELOW_LG] : ([] as string[]);
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches: matching.includes(query),
      addEventListener: () => {},
      removeEventListener: () => {},
    })),
  );
}

function Where() {
  return <output data-where>{useLocation().pathname}</output>;
}

const where = () => document.querySelector("[data-where]")?.textContent;
const menuItems = () => [
  ...document.body.querySelectorAll<HTMLElement>('[role="menuitem"], [role="menuitemradio"]'),
];

async function bar(
  path: string,
  {
    /** `null`: the deployment has not said yet whether it runs many organizations. */
    multiOrg = true as boolean | null,
    organizations = [org("default", "Northwind", 2), org("acme", "Acme", 1)],
  } = {},
) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Number.POSITIVE_INFINITY } },
  });
  holding(qc);
  qc.setQueryData(keys.organizations, organizations);
  if (multiOrg !== null) {
    qc.setQueryData(keys.authProviders, {
      google: false,
      oidc: false,
      oidc_label: "SSO",
      password: true,
      signup: multiOrg,
      multi_org: multiOrg,
    });
  }
  qc.setQueryData(keys.project(PROJECT_ID), project());
  qc.setQueryData(keys.projects, [
    { ...project(), runtime: null },
    OTHER,
    { ...OTHER, id: 3, name: "Paused toy", active: false },
  ]);
  qc.setQueryData(keys.ticket(204), { id: 204, project_id: PROJECT_ID } as TicketDetail);
  view = await mount(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <AssistantProvider>
          <Where />
          <TopNav navLabel="Collapse navigation" navExpanded onToggleNav={() => {}} />
        </AssistantProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return view.container.querySelector("header") as HTMLElement;
}

const orgSwitcher = (root: HTMLElement) => root.querySelector<HTMLElement>("[data-org-switcher]");
const projectSwitcher = (root: HTMLElement) =>
  root.querySelector<HTMLElement>("[data-project-switcher]");
const orgName = (root: HTMLElement) => root.querySelector<HTMLElement>("[data-org-name]");
const projectName = (root: HTMLElement) => root.querySelector<HTMLElement>("[data-project-home]");

describe("the top bar", () => {
  beforeEach(() => width("wide"));

  it("names the organization, then the project inside one, each beside the button that switches it", async () => {
    const root = await bar(`/projects/${PROJECT_ID}/tickets`);
    expect(orgName(root)?.textContent).toBe("Northwind");
    expect(orgSwitcher(root)?.getAttribute("aria-label")).toBe("Switch organization: Northwind");
    expect(projectName(root)?.textContent).toBe("Harbor invoices");
    expect(projectSwitcher(root)?.getAttribute("aria-label")).toBe(
      "Switch project: Harbor invoices",
    );
    // The name is not the menu's button: each is its own control, with its own hover.
    expect(orgName(root)?.tagName).toBe("A");
    expect(orgName(root)?.contains(orgSwitcher(root))).toBe(false);
    expect(projectName(root)?.contains(projectSwitcher(root))).toBe(false);
    const buttons = [...root.querySelectorAll("button")];
    expect(buttons.indexOf(orgSwitcher(root) as HTMLButtonElement)).toBeLessThan(
      buttons.indexOf(projectSwitcher(root) as HTMLButtonElement),
    );
    // The product is the mark's name; the room beside it is the place's.
    expect(root.querySelector('a[href="/overview"]')?.textContent).toBe("Factorial");
    expect(root.textContent).not.toContain("Ask AI");
  });

  it("names the organization alone outside a project", async () => {
    const root = await bar("/overview");
    expect(orgName(root)?.textContent).toBe("Northwind");
    expect(projectSwitcher(root)).toBeNull();
  });

  it("names the ticket's project on a ticket's page", async () => {
    const root = await bar("/tickets/204");
    expect(projectName(root)?.textContent).toBe("Harbor invoices");
  });

  it("goes to the organization's overview from its name, without opening the menu", async () => {
    const root = await bar(`/projects/${PROJECT_ID}/tickets`);
    expect(orgName(root)?.getAttribute("href")).toBe("/overview");
    await click(orgName(root));
    expect(where()).toBe("/overview");
    expect(menuItems()).toEqual([]);
  });

  it("goes to the project's dashboard from its name, from any of its pages", async () => {
    for (const path of [`/projects/${PROJECT_ID}/configuration/secrets`, "/tickets/204"]) {
      const root = await bar(path);
      await click(projectName(root));
      expect(where()).toBe(`/projects/${PROJECT_ID}`);
      expect(menuItems()).toEqual([]);
      await view?.unmount();
      view = null;
    }
  });

  it("opens each menu from its chevron, and says the menu is open there", async () => {
    const root = await bar(`/projects/${PROJECT_ID}/tickets`);
    expect(orgSwitcher(root)?.getAttribute("aria-haspopup")).toBe("menu");
    await click(projectSwitcher(root));
    expect(projectSwitcher(root)?.getAttribute("aria-expanded")).toBe("true");
    expect(menuItems().length).toBeGreaterThan(0);
    expect(where()).toBe(`/projects/${PROJECT_ID}/tickets`);
  });

  it("moves to another organization with a full load, and opens Create organization as a page", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    const root = await bar("/overview");
    await click(orgSwitcher(root));
    expect(menuItems().map((item) => item.textContent)).toEqual([
      expect.stringContaining("Acme"),
      expect.stringContaining("Northwind"),
      "Create organization",
      "Join an organization",
    ]);
    await click(menuItems().find((item) => item.textContent?.startsWith("Northwind")));
    expect(assign).not.toHaveBeenCalled();
    await click(orgSwitcher(root));
    await click(menuItems().find((item) => item.textContent?.startsWith("Acme")));
    expect(assign).toHaveBeenCalledWith("/factorial/orgs/acme/overview");

    await click(orgSwitcher(root));
    await click(menuItems().find((item) => item.textContent === "Create organization"));
    expect(where()).toBe("/organizations/new");
    expect(assign).toHaveBeenCalledTimes(1);
  });

  it("opens the page that takes a pasted invite link, outside any organization, with a full load", async () => {
    // Onboarding links to it only for a person in no organization; a member reaches it here.
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    const root = await bar("/overview");
    await click(orgSwitcher(root));
    await click(menuItems().find((item) => item.textContent === "Join an organization"));
    expect(assign).toHaveBeenCalledWith("/factorial/join");
  });

  it("switches to another active project's same page, and offers the list for the rest", async () => {
    const root = await bar(`/projects/${PROJECT_ID}/tickets`);
    await click(projectSwitcher(root));
    expect(menuItems().map((item) => item.textContent)).toEqual([
      expect.stringContaining("Anchor ledgers"),
      expect.stringContaining("Harbor invoices"),
      expect.stringContaining("All projects"),
    ]);
    expect(document.body.textContent).not.toContain("Paused toy");
    await click(menuItems().find((item) => item.textContent?.startsWith("Anchor ledgers")));
    expect(where()).toBe("/projects/2/tickets");
  });

  it("names the one organization without a menu where there is nowhere else to go", async () => {
    const root = await bar("/overview", {
      multiOrg: false,
      organizations: [org("default", "Northwind", 2)],
    });
    expect(orgSwitcher(root)).toBeNull();
    expect(orgName(root)?.textContent).toBe("Northwind");
    expect(orgName(root)?.getAttribute("href")).toBe("/overview");
  });

  it("shows the whole of a one-organization name the bar cuts short, on hover", async () => {
    // With no menu, nothing else on the page says the name in full.
    const long = "Northwind Logistics International Holdings";
    const root = await bar("/overview", {
      multiOrg: false,
      organizations: [org("default", long, 2)],
    });
    const name = root.querySelector<HTMLElement>("[data-org-name]") as HTMLElement;
    const text = name.querySelector<HTMLElement>(".truncate") as HTMLElement;
    Object.defineProperty(text, "scrollWidth", { configurable: true, value: 400 });
    Object.defineProperty(text, "clientWidth", { configurable: true, value: 180 });
    await hover(name);
    await settle(400);
    expect(document.querySelector('[role="tooltip"]')?.textContent).toBe(long);
  });

  it("keeps one organization a name while the deployment has not said whether it runs many", async () => {
    // So a deployment that runs one does not draw a menu on every load, only to take it away.
    const root = await bar("/overview", {
      multiOrg: null,
      organizations: [org("default", "Northwind", 2)],
    });
    expect(orgSwitcher(root)).toBeNull();
    expect(root.querySelector("[data-org-name]")?.textContent).toBe("Northwind");
  });

  it("offers a member of two organizations both, and no Create, where the deployment runs one", async () => {
    const root = await bar("/overview", { multiOrg: false });
    await click(orgSwitcher(root));
    expect(menuItems().map((item) => item.textContent)).toEqual([
      expect.stringContaining("Acme"),
      expect.stringContaining("Northwind"),
    ]);
  });
});

describe("the top bar below 1024px", () => {
  beforeEach(() => width("medium"));

  it("keeps the names and folds the search to an icon, which opens the field in their place", async () => {
    const root = await bar(`/projects/${PROJECT_ID}`);
    expect(orgSwitcher(root)).not.toBeNull();
    expect(projectSwitcher(root)).not.toBeNull();
    expect(root.querySelector("input[type=search]")).toBeNull();
    await click(root.querySelector('button[aria-label="Search"]'));
    expect(root.querySelector("input[type=search]")).not.toBeNull();
    expect(orgSwitcher(root)).toBeNull();
    await click(root.querySelector('button[aria-label="Close search"]'));
    expect(projectName(root)?.textContent).toBe("Harbor invoices");
  });
});

describe("the top bar on a phone", () => {
  beforeEach(() => width("phone"));

  it("names the organization, and the project under it inside one, as labels", async () => {
    const title = (root: HTMLElement) => root.querySelector("[data-place-title]")?.textContent;
    expect(title(await bar("/overview"))).toBe("Northwind");
    await view?.unmount();
    const inside = await bar(`/projects/${PROJECT_ID}`);
    expect(title(inside)).toBe("NorthwindHarbor invoices");
    // The menus are the navigation's on a phone, where a name in the bar is too small to press.
    expect(orgSwitcher(inside)).toBeNull();
    expect(projectSwitcher(inside)).toBeNull();
  });

  it("folds the search to an icon, which opens the field in the names' place", async () => {
    const root = await bar(`/projects/${PROJECT_ID}`);
    expect(root.querySelector("input[type=search]")).toBeNull();
    await click(root.querySelector('button[aria-label="Search"]'));
    expect(root.querySelector("input[type=search]")).not.toBeNull();
    expect(root.querySelector("[data-place-title]")).toBeNull();
    await click(root.querySelector('button[aria-label="Close search"]'));
    expect(root.querySelector("[data-place-title]")?.textContent).toContain("Harbor invoices");
  });
});
