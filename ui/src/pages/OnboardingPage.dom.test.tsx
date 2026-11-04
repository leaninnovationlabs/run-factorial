// @vitest-environment happy-dom
/**
 * Where an account with no organization lands. With many organizations it makes one; with one,
 * nobody makes another, so it waits for an owner's invite (design.md §7.19).
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { Organization } from "../api/types";
import { AuthProvider } from "../auth";
import { type Mounted, mount, settle } from "../testDom";
import { OnboardingPage } from "./accountPages";

let view: Mounted | null = null;
afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.restoreAllMocks();
});

async function onboarding(
  multiOrg: boolean,
  member = false,
  listPending = false,
): Promise<HTMLElement> {
  vi.spyOn(api, "authMe").mockResolvedValue({ email: "operator@example.com" });
  vi.spyOn(api, "listOrganizations").mockImplementation(() =>
    listPending
      ? new Promise<Organization[]>(() => {})
      : Promise.resolve(member ? [{ slug: "default", name: "Default" } as Organization] : []),
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
        <MemoryRouter>
          <OnboardingPage />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
  await settle();
  await settle();
  return view.container;
}

describe("OnboardingPage", () => {
  it("offers to create an organization when the deployment runs many", async () => {
    const page = await onboarding(true);
    expect(page.textContent).toContain("Create an organization");
    expect(page.textContent).not.toContain("a member of this organization yet");
  });

  it("asks for an owner's invite when the deployment runs one", async () => {
    const page = await onboarding(false);
    expect(page.textContent).toContain("You aren't a member of this organization yet");
    expect(page.textContent).not.toContain("Create an organization");
    expect(page.textContent).not.toContain("Join an organization");
  });

  it("shows nothing about membership while the organization list is still loading", async () => {
    const page = await onboarding(false, true, true);
    expect(page.textContent).not.toContain("a member of this organization yet");
  });

  it("moves a member of the one organization into it, with no chooser", async () => {
    const replace = vi.fn();
    vi.spyOn(window, "location", "get").mockReturnValue({ ...window.location, replace });
    const page = await onboarding(false, true);
    expect(replace).toHaveBeenCalledWith(expect.stringContaining("/orgs/default/overview"));
    expect(page.textContent).not.toContain("Join an organization");
    expect(page.textContent).not.toContain("Your organizations");
  });
});
