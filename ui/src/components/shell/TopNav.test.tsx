// @vitest-environment happy-dom
/**
 * The top bar keeps to the product and the search: where the reader is — the organization, the
 * project — is the side navigation's to say, and so is the assistant, a page of its own. On a
 * phone, where the side navigation is off the page, the bar names the place itself.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { holding } from "../../api/organization.fixture";
import { keys } from "../../api/queries";
import { PROJECT_ID, project } from "../../pages/project.fixture";
import { click, type Mounted, mount } from "../../testDom";
import { AssistantProvider } from "../assistant/AssistantProvider";
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

describe("the top bar", () => {
  it("names neither the organization nor the project on a project's page, and offers no Ask AI", async () => {
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: Number.POSITIVE_INFINITY } },
    });
    qc.setQueryData(keys.project(PROJECT_ID), project());
    view = await mount(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[`/projects/${PROJECT_ID}`]}>
          <AssistantProvider>
            <TopNav navLabel="Collapse navigation" navExpanded onToggleNav={() => {}} />
          </AssistantProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const root = view.container;
    expect(root.querySelector("[data-org-switcher]")).toBeNull();
    expect(root.querySelector("[data-project-switcher]")).toBeNull();
    expect(root.textContent).not.toContain("Harbor invoices");
    expect(root.textContent).toContain("Factorial");
    expect(root.textContent).not.toContain("Ask AI");
  });
});

describe("the top bar on a phone", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        matches: query === "(max-width: 767.98px)",
        addEventListener: () => {},
        removeEventListener: () => {},
      })),
    );
  });

  async function phoneBar(path: string) {
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: Number.POSITIVE_INFINITY } },
    });
    holding(qc);
    qc.setQueryData(keys.project(PROJECT_ID), project());
    view = await mount(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[path]}>
          <AssistantProvider>
            <TopNav navLabel="Open navigation" navExpanded={false} />
          </AssistantProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    return view.container;
  }

  it("names the organization, and the project under it inside one", async () => {
    const title = (root: HTMLElement) => root.querySelector("[data-place-title]")?.textContent;
    expect(title(await phoneBar("/overview"))).toBe("Northwind");
    await view?.unmount();
    expect(title(await phoneBar(`/projects/${PROJECT_ID}`))).toBe("NorthwindHarbor invoices");
  });

  it("folds the search to an icon, which opens the field in the names' place", async () => {
    const root = await phoneBar(`/projects/${PROJECT_ID}`);
    expect(root.querySelector("input[type=search]")).toBeNull();
    await click(root.querySelector('button[aria-label="Search"]'));
    expect(root.querySelector("input[type=search]")).not.toBeNull();
    expect(root.querySelector("[data-place-title]")).toBeNull();
    await click(root.querySelector('button[aria-label="Close search"]'));
    expect(root.querySelector("[data-place-title]")?.textContent).toContain("Harbor invoices");
  });
});
