// @vitest-environment happy-dom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { keys } from "../../api/keys";
import type { Organization } from "../../api/types";
import { ORG_STORAGE_KEY, OrgProvider } from "../../org";
import { type Mounted, mount } from "../../testDom";
import { OrganizationGate } from "./OrganizationGate";

let view: Mounted | null = null;
afterEach(async () => {
  await view?.unmount();
  view = null;
  window.localStorage.clear();
});

const organization = (slug: string, name: string): Organization => ({
  slug,
  name,
  project_count: 0,
  runtime_count: 0,
  updated_by: "migration",
  managed_sandbox_limit: null,
  created_at: "2026-10-17T00:00:00Z",
  updated_at: "2026-10-17T00:00:00Z",
  role: null,
});

async function gate(org: string, known: Organization[] | undefined) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  if (known) client.setQueryData(keys.organizations, known);
  view = await mount(
    <QueryClientProvider client={client}>
      <OrgProvider org={org}>
        <OrganizationGate>
          <p data-page>the page</p>
        </OrganizationGate>
      </OrgProvider>
    </QueryClientProvider>,
  );
  return view.container;
}

describe("the organization gate", () => {
  it("shows the page for an organization that exists, and remembers it", async () => {
    const root = await gate("acme", [
      organization("default", "Default"),
      organization("acme", "Acme"),
    ]);
    expect(root.querySelector("[data-page]")).not.toBeNull();
    expect(window.localStorage.getItem(ORG_STORAGE_KEY)).toBe("acme");
  });

  it("says a slug no organization carries, with a full-load link to each that does", async () => {
    const root = await gate("gone", [organization("default", "Default")]);
    expect(root.querySelector("[data-page]")).toBeNull();
    expect(root.textContent).toContain("There is no organization named gone");
    const link = root.querySelector("a");
    expect(link?.getAttribute("href")).toBe("/factorial/orgs/default/overview");
    expect(window.localStorage.getItem(ORG_STORAGE_KEY)).toBeNull();
  });

  it("sends a reader in no organization to onboarding", async () => {
    const replace = vi.spyOn(window.location, "replace").mockImplementation(() => {});
    const root = await gate("default", []);
    expect(root.querySelector("[data-page]")).toBeNull();
    expect(replace).toHaveBeenCalledWith("/factorial/onboarding");
    replace.mockRestore();
  });

  it("moves a reader who is not in default into their first organization", async () => {
    const replace = vi.spyOn(window.location, "replace").mockImplementation(() => {});
    await gate("default", [organization("acme", "Acme")]);
    expect(replace).toHaveBeenCalledWith("/factorial/orgs/acme/overview");
    replace.mockRestore();
  });
});
