// @vitest-environment happy-dom
/**
 * Creating an organization from inside one: a page of its own, holding the form and nothing
 * else, whose Cancel goes back to the organization's overview (#460).
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { keys } from "../api/keys";
import type { Organization } from "../api/types";
import { byText, click, type Mounted, mount, settle, type } from "../testDom";
import NewOrganizationPage from "./NewOrganizationPage";

let view: Mounted | null = null;
afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.restoreAllMocks();
});

async function page(multiOrg: boolean): Promise<HTMLElement> {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(keys.authProviders, {
    google: false,
    oidc: false,
    oidc_label: "",
    password: true,
    signup: multiOrg,
    multi_org: multiOrg,
  });
  view = await mount(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/organizations/new"]}>
        <NewOrganizationPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await settle();
  return view.container;
}

const field = (root: HTMLElement, label: string) => {
  const found = [...root.querySelectorAll("label")].find((each) =>
    each.textContent?.startsWith(label),
  );
  return found ? root.querySelector<HTMLInputElement>(`#${CSS.escape(found.htmlFor)}`) : null;
};

describe("NewOrganizationPage", () => {
  it("holds the create form alone: no list of organizations and no invite", async () => {
    const root = await page(true);
    expect(root.querySelector("h1")?.textContent).toBe("Create organization");
    expect(field(root, "Name")).not.toBeNull();
    expect(field(root, "Address")).not.toBeNull();
    expect(root.textContent).not.toContain("Your organizations");
    expect(root.textContent).not.toContain("Invite link");
    expect(root.textContent).not.toContain("Get started");
  });

  it("goes back to the overview on Cancel", async () => {
    const root = await page(true);
    expect(byText(root, "a", "Cancel")?.getAttribute("href")).toBe("/overview");
  });

  it("suggests the address from the name, then opens the new organization with a full load", async () => {
    const assign = vi.fn();
    vi.spyOn(window, "location", "get").mockReturnValue({ ...window.location, assign });
    const create = vi
      .spyOn(api, "createOrganization")
      .mockResolvedValue({ slug: "initech", name: "Initech" } as Organization);
    const root = await page(true);
    await type(field(root, "Name"), "Initech");
    expect(field(root, "Address")?.value).toBe("initech");
    await click(byText(root, "button", "Create organization"));
    await settle();
    expect(create).toHaveBeenCalledWith({ name: "Initech", slug: "initech" });
    expect(assign).toHaveBeenCalledWith("/factorial/orgs/initech/overview");
  });

  it("is not found where the deployment runs one organization, as the API's route is", async () => {
    const root = await page(false);
    expect(root.querySelector("h1")?.textContent).toBe("Page not found");
    expect(field(root, "Name")).toBeNull();
    // The tab says what the page does, not what the address asked for.
    expect(document.title).toMatch(/^Page not found/);
  });

  it("names the tab after the form where the deployment runs many", async () => {
    await page(true);
    expect(document.title).toMatch(/^Create organization/);
  });
});
