// @vitest-environment happy-dom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { holding, MEMBER_PERMISSIONS } from "../../api/organization.fixture";
import { keys } from "../../api/queries";
import type { Organization, ProjectListItem, TicketDetail } from "../../api/types";
import { PROJECT_ID, project, readyRows } from "../../pages/project.fixture";
import { byText, click, type Mounted, mount, press } from "../../testDom";
import { ThemeProvider } from "../../theme";
import { AssistantProvider } from "../assistant/AssistantProvider";
import { itemActive, SideNav } from "./SideNav";

let view: Mounted | null = null;
afterEach(async () => {
  await view?.unmount();
  view = null;
  // A group's fold and the theme are kept in the browser: one test's must not decide the next one's.
  localStorage.clear();
  delete document.documentElement.dataset.theme;
});

const PROJECTS = {
  to: "/projects",
  icon: "projects" as const,
  label: "Projects",
  match: ["/projects", "/tickets"],
};

describe("itemActive", () => {
  it("lights Projects on a project, a ticket, and the list itself", () => {
    for (const path of ["/projects", "/projects/12", "/projects/new", "/tickets/204"]) {
      expect(itemActive(PROJECTS, path), path).toBe(true);
    }
  });

  it("does not light on a path that only begins with the same letters", () => {
    expect(itemActive(PROJECTS, "/projects-archive")).toBe(false);
    expect(itemActive({ to: "/overview", icon: "overview", label: "Overview" }, "/projects")).toBe(
      false,
    );
  });
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

const menuItems = () => [
  ...document.body.querySelectorAll<HTMLElement>('[role="menuitem"], [role="menuitemradio"]'),
];

function Where() {
  return <output data-where>{useLocation().pathname}</output>;
}

const where = () => document.querySelector("[data-where]")?.textContent;

async function nav(
  path: string,
  mode: "full" | "compact" | "overlay",
  onClose = () => {},
  onSignOut = () => {},
  seed: (qc: QueryClient) => void = () => {},
) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Number.POSITIVE_INFINITY } },
  });
  // Nothing answers: the navigation must stand up without the API.
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.reject(new TypeError("offline"))),
  );
  // The page's organization is `default`, the one `useOrg` answers outside a provider.
  qc.setQueryData(keys.organizations, [org("default", "Northwind", 2), org("acme", "Acme", 1)]);
  holding(qc);
  seed(qc);
  view = await mount(
    <ThemeProvider>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[path]}>
          <AssistantProvider>
            <Where />
            <SideNav
              mode={mode}
              onClose={onClose}
              sessionEmail="operator@example.com"
              onSignOut={onSignOut}
            />
          </AssistantProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </ThemeProvider>,
  );
  return view.container;
}

/** The project the navigation is in, with another active project to switch to. */
function inProject(over: Parameters<typeof project>[0] = {}) {
  return (qc: QueryClient) => {
    qc.setQueryData(keys.project(PROJECT_ID), project({ readiness: readyRows(), ...over }));
    qc.setQueryData(keys.projects, [
      { ...project(over), runtime: null },
      OTHER,
      { ...OTHER, id: 3, name: "Paused toy", active: false },
    ]);
  };
}

const links = (root: HTMLElement) => [...root.querySelectorAll<HTMLAnchorElement>("a")];
const labels = (root: HTMLElement) =>
  links(root).map((link) => link.getAttribute("aria-label") ?? link.textContent);

describe("SideNav", () => {
  it("marks the page the reader is on as current, in shape as well as colour", async () => {
    const root = await nav("/runtimes/codex", "full");
    const current = root.querySelector('[aria-current="page"]');
    expect(current?.textContent).toBe("Runtimes");
    expect(current?.querySelector(".bg-neutral-bold")).not.toBeNull();
    expect(root.querySelectorAll('[aria-current="page"]')).toHaveLength(1);
  });

  it("names a compact item by its label, the icon being all that is drawn", async () => {
    const root = await nav("/overview", "compact");
    expect(labels(root)).toEqual([
      "Overview",
      "Projects",
      "Assistant",
      "Runtimes",
      "MCP access",
      "Assistant settings",
      "Members",
    ]);
    expect(links(root).every((link) => link.textContent === "")).toBe(true);
    expect(root.querySelector("[title]")).toBeNull();
  });

  it("keeps the organization, help and the account at its foot, in that order, each named by its label when compact", async () => {
    const root = await nav("/overview", "compact");
    const menus = [...root.querySelectorAll('[aria-haspopup="menu"]')];
    expect(menus.map((menu) => menu.getAttribute("aria-label"))).toEqual([
      "Switch organization: Northwind",
      "Help",
      "Account",
    ]);
    expect(menus.every((menu) => menu.textContent === "")).toBe(true);
  });

  it("names the organization first at its foot, and moves to another with a full load", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    const root = await nav("/overview", "full");
    const menus = [...root.querySelectorAll<HTMLElement>('[aria-haspopup="menu"]')];
    const switcher = menus[0];
    expect(switcher?.hasAttribute("data-org-switcher")).toBe(true);
    expect(switcher?.textContent).toBe("Northwind");

    await click(switcher);
    expect(menuItems().map((item) => item.textContent)).toEqual([
      expect.stringContaining("Acme"),
      expect.stringContaining("Northwind"),
    ]);
    await click(menuItems().find((item) => item.textContent?.startsWith("Northwind")));
    expect(assign).not.toHaveBeenCalled();
    await click(switcher);
    await click(menuItems().find((item) => item.textContent?.startsWith("Acme")));
    expect(assign).toHaveBeenCalledWith("/factorial/orgs/acme/overview");
    vi.unstubAllGlobals();
  });

  it("signs out from the account menu, which says who is signed in", async () => {
    const onSignOut = vi.fn();
    const root = await nav("/overview", "full", () => {}, onSignOut);
    await click(byText(root, '[aria-haspopup="menu"]', "Account"));
    expect(document.body.textContent).toContain("Signed in as operator@example.com");
    await click(byText(document.body, '[role="menuitem"]', "Sign out"));
    expect(onSignOut).toHaveBeenCalledTimes(1);
  });

  it("opens over the page as a modal layer that Escape closes, focus on its first item", async () => {
    const onClose = vi.fn();
    const root = await nav("/overview", "overlay", onClose);
    expect(document.activeElement?.textContent).toBe("Overview");
    // Outside the page it covers, which is what lets the page go inert without taking it along.
    expect(root.contains(document.getElementById("side-nav"))).toBe(false);
    expect(root.hasAttribute("inert")).toBe(true);
    expect(document.body.querySelector('[aria-hidden="true"].bg-blanket')).not.toBeNull();
    await press("Escape");
    expect(onClose).toHaveBeenCalledTimes(1);
    // It covers the top bar's menu button, so it carries its own way out.
    await click(document.body.querySelector('[aria-label="Close navigation"]'));
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});

describe("the theme", () => {
  it("is a row of icons in the account menu, one press each, never a menu of its own", async () => {
    const root = await nav("/overview", "full");
    // Nothing about the theme sits in the navigation itself.
    expect(root.querySelector('[aria-label="Theme"]')).toBeNull();
    const account = () => byText(root, '[aria-haspopup="menu"]', "Account");
    const choice = (name: string) =>
      document.body.querySelector<HTMLElement>(
        `[role="group"][aria-label="Theme"] [role="menuitemradio"][aria-label="${name}"]`,
      );

    await click(account());
    expect(
      [
        ...document.body.querySelectorAll(
          '[role="group"][aria-label="Theme"] [role="menuitemradio"]',
        ),
      ].map((item) => item.getAttribute("aria-label")),
    ).toEqual(["Light", "Dark", "Match system theme"]);
    expect(choice("Match system theme")?.getAttribute("aria-checked")).toBe("true");
    expect(document.body.querySelector('[aria-haspopup="menu"][data-submenu="Theme"]')).toBeNull();

    await click(choice("Dark"));
    expect(document.documentElement.dataset.theme).toBe("dark");
    await click(account());
    expect(choice("Dark")?.getAttribute("aria-checked")).toBe("true");
    await click(choice("Match system theme"));
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });
});

describe("SideNav in the organization", () => {
  afterEach(() => localStorage.clear());

  it("names the organization's places, then a Settings group that starts folded", async () => {
    const root = await nav("/overview", "full");
    expect(labels(root)).toEqual([
      "Overview",
      "Projects",
      "Assistant",
      "Runtimes",
      "MCP access",
      "Assistant settings",
      "Members",
    ]);
    expect(byText(root, "a", "Assistant")?.getAttribute("href")).toBe("/assistant");
    const toggle = byText(root, "button", "Settings");
    expect(toggle?.getAttribute("aria-expanded")).toBe("false");
    expect(document.getElementById("nav-settings-pages")?.hidden).toBe(true);

    await click(toggle);
    expect(toggle?.getAttribute("aria-expanded")).toBe("true");
    expect(document.getElementById("nav-settings-pages")?.hidden).toBe(false);
    expect(localStorage.getItem("sf-ui:nav-settings-open")).toBe("1");
  });

  it("unfolds Settings on one of its pages, and marks that page current", async () => {
    const root = await nav("/account/mcp", "full");
    expect(document.getElementById("nav-settings-pages")?.hidden).toBe(false);
    expect(root.querySelector('[aria-current="page"]')?.textContent).toBe("MCP access");
  });

  it("gives each settings page its icon, as a project's configuration pages have theirs", async () => {
    const root = await nav("/runtimes", "full");
    const pages = root.querySelectorAll("#nav-settings-pages a");
    expect(
      [...pages].map((link) => link.querySelector("[data-glyph]")?.getAttribute("data-glyph")),
    ).toEqual(["runtimes", "key", "sparkles", "users"]);
  });

  it("shows a member only the settings pages their role reaches", async () => {
    const root = await nav("/overview", "full", undefined, undefined, (qc) =>
      holding(qc, MEMBER_PERMISSIONS),
    );
    expect(labels(root)).toEqual(["Overview", "Projects", "Assistant", "MCP access"]);
  });

  it("draws no Settings group for a reader whose role reaches none of its pages", async () => {
    const root = await nav("/overview", "full", undefined, undefined, (qc) =>
      holding(qc, ["org:read"]),
    );
    expect(labels(root)).toEqual(["Overview", "Projects"]);
    expect(byText(root, "button", "Settings")).toBeFalsy();
  });
});

describe("SideNav inside a project", () => {
  afterEach(() => localStorage.clear());

  it("becomes the project's: its pages, and its configuration unfolded on a configuration page", async () => {
    const root = await nav(
      `/projects/${PROJECT_ID}/configuration/secrets`,
      "full",
      undefined,
      undefined,
      inProject(),
    );
    expect(labels(root)).toEqual([
      "Dashboard",
      "Tickets",
      "Settings",
      "Details",
      "Git source",
      "Process",
      "Tickets",
      "Runtime",
      "Secrets",
    ]);
    // Each settings page carries the glyph the app uses for the same thing elsewhere.
    const pages = root.querySelectorAll("#nav-configuration-pages a");
    expect(
      [...pages].map((link) => link.querySelector("[data-glyph]")?.getAttribute("data-glyph")),
    ).toEqual(["edit", "branch", "lane", "tickets", "runtimes", "key"]);
    const current = [...root.querySelectorAll('[aria-current="page"]')];
    expect(current.map((each) => each.textContent)).toEqual(["Secrets"]);
    expect(current[0]?.getAttribute("href")).toBe(`/projects/${PROJECT_ID}/configuration/secrets`);
    // The organization's places are gone.
    expect(root.textContent).not.toContain("Overview");
    expect(root.textContent).not.toContain("Runtimes");
  });

  it("names the project first, on a filled band across the navigation, as the button that switches it", async () => {
    const root = await nav(
      `/projects/${PROJECT_ID}/tickets`,
      "full",
      undefined,
      undefined,
      inProject(),
    );
    const buttons = [...root.querySelectorAll<HTMLElement>("#side-nav a, #side-nav button")];
    const first = buttons[0];
    expect(first?.hasAttribute("data-project-switcher")).toBe(true);
    expect(first?.textContent).toBe("Harbor invoices");
    expect(first?.getAttribute("aria-label")).toBe("Switch project: Harbor invoices");
    const band = first?.closest("[data-project-band]");
    expect(band?.className).toContain("bg-neutral ");
    // Out past the navigation's padding, and outside the region the items scroll in.
    expect(band?.className).toContain("-mx-2");
    expect(band?.parentElement?.id).toBe("side-nav");
  });

  it("shows a member the project's Settings, which they may read", async () => {
    const root = await nav(`/projects/${PROJECT_ID}`, "full", undefined, undefined, (qc) => {
      inProject()(qc);
      holding(qc, MEMBER_PERMISSIONS);
    });
    expect(labels(root)).toEqual([
      "Dashboard",
      "Tickets",
      "Settings",
      "Details",
      "Git source",
      "Process",
      "Tickets",
      "Runtime",
      "Secrets",
    ]);
    expect(byText(root, "button", "Assistant")).toBeTruthy();
  });

  it("shows a reader without projects:read the project's pages without its Settings", async () => {
    const root = await nav(`/projects/${PROJECT_ID}`, "full", undefined, undefined, (qc) => {
      inProject()(qc);
      holding(
        qc,
        MEMBER_PERMISSIONS.filter((permission) => permission !== "projects:read"),
      );
    });
    expect(labels(root)).toEqual(["Dashboard", "Tickets"]);
    expect(root.querySelector("#nav-configuration-pages")).toBeNull();
  });

  it("offers the assistant as a Drawer over the page, not a page of its own", async () => {
    const root = await nav(`/projects/${PROJECT_ID}`, "full", undefined, undefined, inProject());
    const assistant = byText(root, "button", "Assistant");
    expect(assistant?.getAttribute("aria-haspopup")).toBe("dialog");
    expect(links(root).some((link) => link.getAttribute("href") === "/assistant")).toBe(false);
  });

  it("switches to another active project's same page, and offers the list for the rest", async () => {
    const root = await nav(
      `/projects/${PROJECT_ID}/tickets`,
      "full",
      undefined,
      undefined,
      inProject(),
    );
    await click(root.querySelector("[data-project-switcher]"));
    expect(menuItems().map((item) => item.textContent)).toEqual([
      expect.stringContaining("Anchor ledgers"),
      expect.stringContaining("Harbor invoices"),
      expect.stringContaining("All projects"),
    ]);
    expect(document.body.textContent).not.toContain("Paused toy");
    await click(menuItems().find((item) => item.textContent?.startsWith("Anchor ledgers")));
    expect(where()).toBe("/projects/2/tickets");
  });

  it("folds the configuration pages away, keeping Configuration current for the page it hides", async () => {
    const root = await nav(
      `/projects/${PROJECT_ID}/configuration/secrets`,
      "full",
      undefined,
      undefined,
      inProject(),
    );
    const toggle = root.querySelector<HTMLElement>('[aria-controls="nav-configuration-pages"]');
    expect(toggle?.getAttribute("aria-expanded")).toBe("true");
    expect(toggle?.getAttribute("aria-label")).toBe("Collapse Settings");

    await click(toggle);
    expect(toggle?.getAttribute("aria-expanded")).toBe("false");
    expect(document.getElementById("nav-configuration-pages")?.hidden).toBe(true);
    const current = root.querySelector('[aria-current="page"]');
    expect(current?.textContent).toBe("Settings");
    // The fold is kept for the next page and the next visit.
    expect(localStorage.getItem("sf-ui:nav-configuration-open")).toBe("0");
  });

  it("starts folded outside the configuration, and unfolds when the reader goes into it", async () => {
    const root = await nav(`/projects/${PROJECT_ID}`, "full", undefined, undefined, inProject());
    expect(document.getElementById("nav-configuration-pages")?.hidden).toBe(true);

    await click(byText(root, "a", "Settings"));
    expect(where()).toBe(`/projects/${PROJECT_ID}/configuration`);
    expect(document.getElementById("nav-configuration-pages")?.hidden).toBe(false);
  });

  it("compact, draws the project's pages alone and lets Configuration stand for every section", async () => {
    const root = await nav(
      `/projects/${PROJECT_ID}/configuration/secrets`,
      "compact",
      undefined,
      undefined,
      inProject(),
    );
    expect(labels(root)).toEqual(["Dashboard", "Tickets", "Settings"]);
    expect(root.querySelector('[aria-current="page"]')?.getAttribute("aria-label")).toBe(
      "Settings",
    );
    expect(root.querySelector("[data-project-switcher]")?.getAttribute("aria-label")).toBe(
      "Switch project: Harbor invoices",
    );
    expect(root.querySelector('[aria-controls="nav-configuration-pages"]')).toBeNull();
  });

  it("marks a requirement that blocks activation on its row and counts it on Configuration", async () => {
    localStorage.setItem("sf-ui:nav-configuration-open", "1");
    const root = await nav(
      `/projects/${PROJECT_ID}`,
      "full",
      undefined,
      undefined,
      inProject({
        blockers: ["Set the ticket source."],
        readiness: [
          {
            key: "ticket_source",
            process: null,
            runtime_slug: null,
            state: "blocked",
            summary: "No ticket source",
            detail: null,
            remedy: null,
            link: null,
          },
        ],
      }),
    );
    const configuration = links(root).find((link) => link.textContent?.startsWith("Settings"));
    expect(configuration?.textContent).toBe("Settings1");
    // "Tickets" names the project's tickets too; the settings page is found by where it goes.
    const source = root.querySelector(
      `a[href="/projects/${PROJECT_ID}/configuration/ticket-source"]`,
    );
    expect(source?.querySelector('[aria-label="Blocks activation"]')).not.toBeNull();
    expect(byText(root, "a", "Dashboard")?.getAttribute("aria-current")).toBe("page");
  });

  it("does not run the repo-side probes itself, and folded, carries a warning they found", async () => {
    const fetchMock = vi.fn(() => Promise.reject(new TypeError("offline")));
    const root = await nav(`/projects/${PROJECT_ID}`, "full", undefined, undefined, (qc) => {
      inProject()(qc);
      qc.setQueryData(keys.projectReadiness(PROJECT_ID), {
        readiness: [
          {
            key: "labels",
            process: null,
            runtime_slug: null,
            state: "warn",
            summary: "2 labels are missing",
            detail: null,
            remedy: null,
            link: null,
          },
        ],
        warnings: [],
      });
      // Read once a page load, for the organization switcher: answered here so the nav asks nothing.
      qc.setQueryData(keys.authProviders, { google: false, oidc: false, oidc_label: "SSO" });
      qc.setQueryData(keys.version, { revision: null });
      vi.stubGlobal("fetch", fetchMock);
    });
    expect(fetchMock).not.toHaveBeenCalled();
    const configuration = links(root).find((link) => link.textContent?.startsWith("Settings"));
    expect(configuration?.querySelector('[aria-label="Needs attention"]')).not.toBeNull();

    await click(root.querySelector('[aria-controls="nav-configuration-pages"]'));
    expect(configuration?.querySelector('[aria-label="Needs attention"]')).toBeNull();
    expect(
      byText(root, "a", "Ticket labels")?.querySelector('[aria-label="Needs attention"]'),
    ).not.toBeNull();
  });

  it("marks Git Source as an error when the App cannot see the repository", async () => {
    localStorage.setItem("sf-ui:nav-configuration-open", "1");
    const root = await nav(`/projects/${PROJECT_ID}`, "full", undefined, undefined, (qc) => {
      inProject()(qc);
      qc.setQueryData(keys.projectReadiness(PROJECT_ID), {
        readiness: [
          {
            key: "app_access",
            process: null,
            runtime_slug: null,
            state: "blocked",
            summary: "acme/widgets is not in reach",
            detail: null,
            remedy: null,
            link: null,
          },
        ],
        warnings: [],
      });
    });
    const setup = byText(root, "a", "Git source");
    expect(setup?.querySelector('[aria-label="Cannot run"]')).not.toBeNull();
  });

  it("is the ticket's project's on a ticket's page, once the ticket says which", async () => {
    const root = await nav("/tickets/204", "full", undefined, undefined, (qc) => {
      inProject()(qc);
      qc.setQueryData(keys.ticket(204), { id: 204, project_id: PROJECT_ID } as TicketDetail);
    });
    expect(byText(root, "a", "Tickets")?.getAttribute("aria-current")).toBe("page");
    expect(byText(root, "a", "Dashboard")?.getAttribute("href")).toBe(`/projects/${PROJECT_ID}`);
    expect(root.querySelector("[data-project-switcher]")?.textContent).toBe("Harbor invoices");
  });
});
