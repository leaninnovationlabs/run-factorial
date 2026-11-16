// @vitest-environment happy-dom
/**
 * The frame, pressed: the skip link, the page an unknown address gets, the theme choice, the
 * search's jump, and where the focus lands after a page changes.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { keys } from "../../api/queries";
import type { OverviewResponse } from "../../api/types";
import { byText, click, type Mounted, mount, press, settle, type } from "../../testDom";
import { ThemeProvider } from "../../theme";
import { AppShell, focusPageTitle, navLayout } from "./AppShell";

const OVERVIEW = {
  projects: [{ id: 1, name: "widgets" }],
  tickets: [
    {
      ticket_id: 11,
      project_id: 1,
      external_key: "acme/widgets#118",
      title: "Retry the webhook",
      current_step: "build",
      process_key: "standard",
      latest_run_status: null,
      status: "running",
      runs: 1,
      attempt: 1,
      cost_usd: "0",
      cost_estimated: false,
      tokens: 0,
      minutes: 0,
      quarantined_at: null,
    },
  ],
  done_days: 7,
} as OverviewResponse;

let view: Mounted | null = null;

beforeEach(() => {
  // Nothing answers: the shell must stand up without the API, and pages say so themselves.
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.reject(new TypeError("offline"))),
  );
});

afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.unstubAllGlobals();
  delete document.documentElement.dataset.theme;
});

function Where() {
  return <output data-where>{useLocation().pathname}</output>;
}

async function shell(path: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Number.POSITIVE_INFINITY } },
  });
  client.setQueryData(keys.overview, OVERVIEW);
  client.setQueryData(keys.runtimes, []);
  client.setQueryData(keys.projects, []);
  view = await mount(
    <ThemeProvider>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[path]}>
          <Where />
          <AppShell sessionEmail="operator@example.com" onSignOut={() => {}} />
        </MemoryRouter>
      </QueryClientProvider>
    </ThemeProvider>,
  );
  return view.container;
}

const where = (root: HTMLElement) => root.querySelector("[data-where]")?.textContent;

describe("AppShell", () => {
  it("offers a skip link first, to the main region", async () => {
    const root = await shell("/projects");
    const first = root.querySelector("a");
    expect(first?.textContent).toBe("Skip to main content");
    expect(first?.getAttribute("href")).toBe("#main");
    expect(root.querySelector("main#main")).not.toBeNull();
  });

  it("draws Create organization without the side navigation, which returns on the next page", async () => {
    // Creating an organization is about none of the current one's places.
    const root = await shell("/organizations/new");
    expect(root.querySelector('nav[aria-label="Primary"]')).toBeNull();
    expect(root.querySelector('header button[aria-controls="side-nav"]')).toBeNull();
    await click(byText(root, "header a", "Factorial"));
    await settle(400);
    expect(where(root)).toBe("/overview");
    expect(root.querySelector('nav[aria-label="Primary"]')).not.toBeNull();
    expect(root.querySelector('header button[aria-controls="side-nav"]')).not.toBeNull();
  });

  it("says so for an address the app does not have, instead of redirecting in silence", async () => {
    const root = await shell("/somewhere-else");
    expect(root.querySelector("h1")?.textContent).toBe("Page not found");
    expect(byText(root, "a", "Go to Overview")?.getAttribute("href")).toBe("/overview");
    expect(where(root)).toBe("/somewhere-else");
  });

  it("switches theme from the account menu's icons, and 'Match system theme' takes the choice away", async () => {
    const root = await shell("/projects");
    // At the side navigation's foot, not in the top bar.
    const account = () => byText(root, '#side-nav [aria-haspopup="menu"]', "Account");
    const theme = (name: string) =>
      document.body.querySelector<HTMLElement>(`[role="menuitemradio"][aria-label="${name}"]`);
    expect(root.querySelector("header")?.textContent).not.toContain("Account");
    await click(account());
    expect(document.body.textContent).toContain("Signed in as operator@example.com");
    await click(theme("Dark"));
    expect(document.documentElement.dataset.theme).toBe("dark");

    await click(account());
    expect(theme("Dark")?.getAttribute("aria-checked")).toBe("true");
    await click(theme("Match system theme"));
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  it("jumps to a ticket by its number from the search, with the keyboard alone", async () => {
    const root = await shell("/projects");
    const field = root.querySelector<HTMLInputElement>('[role="combobox"]');
    field?.focus();
    await type(field, "118");
    const option = root.querySelector('[role="option"]');
    expect(option?.textContent).toContain("#118 Retry the webhook");
    expect(field?.getAttribute("aria-activedescendant")).toBe(option?.id);
    await press("Enter", field ?? document);
    expect(where(root)).toBe("/tickets/11");
  });

  it("reaches the search on a chord, never on a character a speech user might dictate", async () => {
    const root = await shell("/projects");
    const field = root.querySelector('[role="combobox"]');
    await press("/");
    expect(document.activeElement).not.toBe(field);
    await press("k", document, { ctrlKey: true });
    expect(document.activeElement).toBe(field);
  });

  it("puts the focus on the new page's title after a navigation, not on the link pressed", async () => {
    const root = await shell("/runtimes/new");
    await click(byText(root, "a", "Projects"));
    // The outgoing page fades before the new one mounts.
    await settle(250);
    expect(document.activeElement?.tagName).toBe("H1");
    expect(document.activeElement?.textContent).toBe("Projects");
  });
});

describe("focusPageTitle", () => {
  /** The main region, and a title to put in it once the page's chunk has loaded. */
  function region() {
    const main = document.createElement("main");
    main.tabIndex = -1;
    document.body.append(main);
    const title = document.createElement("h1");
    title.tabIndex = -1;
    title.dataset.pageTitle = "";
    return { main, title };
  }

  afterEach(() => {
    document.body.replaceChildren();
  });

  it("holds the focus on the region while the page loads, then gives it to the title", async () => {
    const { main, title } = region();
    const stop = focusPageTitle(main);
    expect(document.activeElement).toBe(main);
    main.append(title);
    await settle();
    expect(document.activeElement).toBe(title);
    stop();
  });

  it("leaves the focus where the reader put it while the page loaded", async () => {
    const { main, title } = region();
    const elsewhere = document.createElement("button");
    document.body.append(elsewhere);
    const stop = focusPageTitle(main);
    elsewhere.focus();
    main.append(title);
    await settle();
    expect(document.activeElement).toBe(elsewhere);
    stop();
  });
});

describe("navLayout", () => {
  it("keeps the reader's choice of full or compact on a wide screen", () => {
    expect(navLayout(false, false, false, false)).toEqual({
      mode: "full",
      label: "Collapse navigation",
      expanded: true,
    });
    expect(navLayout(false, false, true, false).mode).toBe("compact");
  });

  it("goes compact below 1024px and off the page below 768px, the button opening it over", () => {
    expect(navLayout(false, true, false, false).mode).toBe("compact");
    expect(navLayout(true, true, false, false)).toEqual({
      mode: "hidden",
      label: "Open navigation",
      expanded: false,
    });
    expect(navLayout(true, true, false, true)).toEqual({
      mode: "overlay",
      label: "Close navigation",
      expanded: true,
    });
  });
});
