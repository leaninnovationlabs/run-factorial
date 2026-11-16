// @vitest-environment happy-dom
/**
 * Where an account with no organization lands, and where it pastes an invite's link. With many
 * organizations onboarding makes one, and joining is a page of its own; with one, nobody makes
 * another, so it waits for an owner's invite. A member of any organization goes into the first
 * (design.md §7.19).
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { Organization } from "../api/types";
import { AuthProvider } from "../auth";
import { forgetOrganization, rememberOrganization } from "../org";
import { byText, click, type Mounted, mount, settle, type } from "../testDom";
import { JoinPage, OnboardingPage } from "./accountPages";

let view: Mounted | null = null;
afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.restoreAllMocks();
  forgetOrganization();
});

const NORTHWIND = { slug: "default", name: "Default" } as Organization;
const ACME = { slug: "acme", name: "Acme" } as Organization;

/** The reader's organizations as the list answers: rows, an answer never sent, or a failure. */
type Listed = Organization[] | "pending" | "failed";

async function render(
  page: "onboarding" | "join",
  multiOrg: boolean,
  organizations: Listed = [],
): Promise<HTMLElement> {
  vi.spyOn(api, "authMe").mockResolvedValue({ email: "operator@example.com" });
  vi.spyOn(api, "listOrganizations").mockImplementation(() =>
    organizations === "pending"
      ? new Promise<Organization[]>(() => {})
      : organizations === "failed"
        ? Promise.reject(new TypeError("offline"))
        : Promise.resolve(organizations),
  );
  vi.spyOn(api, "authProviders").mockResolvedValue({
    google: false,
    oidc: false,
    oidc_label: "",
    password: true,
    signup: multiOrg,
    multi_org: multiOrg,
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  view = await mount(
    <QueryClientProvider client={client}>
      <AuthProvider>
        <MemoryRouter>{page === "onboarding" ? <OnboardingPage /> : <JoinPage />}</MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
  await settle();
  await settle();
  return view.container;
}

const onboarding = (multiOrg: boolean, organizations: Listed = []) =>
  render("onboarding", multiOrg, organizations);

function moves() {
  const replace = vi.fn();
  const assign = vi.fn();
  vi.spyOn(window, "location", "get").mockReturnValue({ ...window.location, replace, assign });
  return { replace, assign };
}

describe("OnboardingPage", () => {
  it("offers to create an organization when the deployment runs many, and links to joining one", async () => {
    const page = await onboarding(true);
    expect(page.querySelector("h1")?.textContent).toBe("Create your organization");
    expect(byText(page, "button", "Create organization")).not.toBeNull();
    expect(byText(page, "a", "Join an organization")?.getAttribute("href")).toBe("/factorial/join");
    // Pasting a link is the join page's, not this one's.
    expect(page.textContent).not.toContain("Invite link");
    expect(page.textContent).not.toContain("a member of this organization yet");
  });

  it("asks for an owner's invite when the deployment runs one", async () => {
    const page = await onboarding(false);
    expect(page.textContent).toContain("You aren't a member of this organization yet");
    expect(page.textContent).not.toContain("Create organization");
    expect(page.textContent).not.toContain("Join an organization");
  });

  it("does not call the reader a stranger when their organizations could not be read", async () => {
    const page = await onboarding(false, "failed");
    expect(page.querySelector("h1")?.textContent).toBe("Get started");
    expect(page.textContent).not.toContain("a member of this organization yet");
  });

  it("names the tab after the card it shows", async () => {
    await onboarding(true);
    expect(document.title).toMatch(/^Create your organization/);
  });

  it.each([true, false])(
    "offers Sign out beside the signed-in address (multi-org %s)",
    async (multi) => {
      const page = await onboarding(multi);
      expect(page.textContent).toContain("Signed in as operator@example.com. Not you? Sign out");
      const links = [...page.querySelectorAll("a")].filter((a) => a.textContent === "Sign out");
      expect(links).toHaveLength(1);
    },
  );

  it.each([true, false])(
    "shows nothing while the organization list is still loading (multi-org %s)",
    async (multi) => {
      const page = await onboarding(multi, "pending");
      expect(page.textContent).toBe("");
    },
  );

  it.each([true, false])(
    "moves a member into their first organization, with no list to choose from (multi-org %s)",
    async (multi) => {
      const { replace } = moves();
      const page = await onboarding(multi, [NORTHWIND, ACME]);
      expect(replace).toHaveBeenCalledWith("/factorial/orgs/default/overview");
      expect(page.textContent).not.toContain("Your organizations");
      expect(page.textContent).not.toContain("Create organization");
    },
  );

  it("moves a member into the organization this browser last used, while they are still in it", async () => {
    // An expired invite's "Go to the start page" should not move them out of where they work.
    rememberOrganization("acme");
    const { replace } = moves();
    await onboarding(true, [NORTHWIND, ACME]);
    expect(replace).toHaveBeenCalledWith("/factorial/orgs/acme/overview");
  });

  it("moves a member out of the organization they just left, into their first", async () => {
    // Leaving one lands here, and this browser still remembers the organization left.
    rememberOrganization("initech");
    const { replace } = moves();
    await onboarding(true, [NORTHWIND, ACME]);
    expect(replace).toHaveBeenCalledWith("/factorial/orgs/default/overview");
  });
});

describe("JoinPage", () => {
  const link = (page: HTMLElement) =>
    page.querySelector<HTMLInputElement>(
      `#${CSS.escape(byText(page, "label", "Invite link")?.getAttribute("for") ?? "")}`,
    );

  it("opens the invite a pasted link names", async () => {
    const { assign } = moves();
    const page = await render("join", true);
    expect(page.querySelector("h1")?.textContent).toBe("Join an organization");
    await type(link(page), "https://factory.example.com/factorial/invite/abcdefghij0123456789_-");
    await click(byText(page, "button", "Open invite"));
    expect(assign).toHaveBeenCalledWith("/factorial/invite/abcdefghij0123456789_-");
  });

  it("says so when the pasted text is not an invite's link", async () => {
    const { assign } = moves();
    const page = await render("join", true);
    await type(link(page), "not a link");
    await click(byText(page, "button", "Open invite"));
    expect(page.textContent).toContain("That isn't an invite link.");
    expect(assign).not.toHaveBeenCalled();
  });

  it("offers to create an organization only where people may", async () => {
    const many = await render("join", true);
    expect(byText(many, "a", "create your own organization")?.getAttribute("href")).toBe(
      "/factorial/onboarding",
    );
    await view?.unmount();
    const one = await render("join", false);
    expect(one.textContent).not.toContain("create your own organization");
    expect(one.textContent).toContain("Signed in as operator@example.com");
  });

  it("sends a member who wants an organization of their own to the page that makes one", async () => {
    // Onboarding would only move them into the organization they already have.
    const page = await render("join", true, [NORTHWIND, ACME]);
    expect(byText(page, "a", "create your own organization")?.getAttribute("href")).toBe(
      "/factorial/orgs/default/organizations/new",
    );
  });
});
