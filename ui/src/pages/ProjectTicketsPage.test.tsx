// @vitest-environment happy-dom
/**
 * The tickets page's `Admission` filter, in a DOM.
 *
 * It is the one filter whose rows are not tickets, so what it has to prove is that the table
 * changes shape under it and that every verdict reaches the row it is about: the two panels this
 * replaced stated "nothing in flight" to a reader who had not asked, and reported a misconfigured
 * issue in a banner detached from the row that named it.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { keys } from "../api/queries";
import type { PendingIssue } from "../api/types";
import { APP_PREFIX } from "../prefix";
import { byText, choose, type Mounted, mount, settle } from "../testDom";
import { ProjectTicketsPage } from "./ProjectTicketsPage";
import { PROJECT_ID, project } from "./project.fixture";

const PROJECT = project({ ticket_count: 4, open_ticket_count: 2 });

const WAITING: PendingIssue = {
  external_key: "41",
  title: "Invoice totals round the wrong way",
  process_key: "simple",
  reason: "not_ready",
  detail: "selected simple (the project default) at ab12cd3; its entry condition is not satisfied",
  awaiting: ['ticket_label("factorial:new") = false', "ticket_has_assignee() = false"],
};

const MISCONFIGURED: PendingIssue = {
  external_key: "38",
  title: "Retire the legacy exporter",
  process_key: null,
  reason: "no_default",
  detail: "no default process is set, and the issue carries no process label",
  awaiting: [],
};

const fetchMock = vi.fn();
let view: Mounted | null = null;

beforeEach(() => {
  fetchMock.mockImplementation(() => Promise.reject(new TypeError("offline")));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(async () => {
  await view?.unmount();
  view = null;
  fetchMock.mockReset();
  vi.unstubAllGlobals();
});

async function page(pending: PendingIssue[] | null, shown = PROJECT): Promise<HTMLElement> {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Number.POSITIVE_INFINITY } },
  });
  qc.setQueryData(keys.project(PROJECT_ID), shown);
  // Null seeds no lists, so the page has to ask for them.
  if (pending) {
    qc.setQueryData(keys.pendingIssues(PROJECT_ID), pending);
    qc.setQueryData(keys.tickets(PROJECT_ID), {
      project_id: PROJECT_ID,
      status: null,
      open_only: false,
      tickets: [],
    });
  }
  view = await mount(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/projects/${PROJECT_ID}/tickets`]}>
        <Routes>
          <Route path="/projects/:projectId/tickets" element={<ProjectTicketsPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return view.container;
}

function statusSelect(root: HTMLElement): HTMLSelectElement | null {
  return root.querySelector<HTMLSelectElement>('select[aria-label="Filter tickets by status"]');
}

/** The options carry their count after the word, so a filter is found by what it starts with. */
function filter(root: HTMLElement, label: string): HTMLOptionElement | null {
  const options = [...(statusSelect(root)?.options ?? [])];
  return options.find((each) => (each.textContent ?? "").startsWith(label)) ?? null;
}

async function pick(root: HTMLElement, label: string): Promise<void> {
  await choose(statusSelect(root), filter(root, label)?.value ?? "");
}

function columns(root: HTMLElement): string[] {
  return [...root.querySelectorAll("th")].map((cell) => cell.textContent?.trim() ?? "");
}

describe("the tickets page", () => {
  it("names itself, and reads the tickets and the open issues", async () => {
    const root = await page(null);
    expect(root.querySelector("h1")?.textContent).toBe("Tickets");
    await settle();
    const paths = fetchMock.mock.calls.map(([url]) =>
      new URL(String(url), "http://api").pathname.slice(APP_PREFIX.length),
    );
    expect(paths).toContain(`/api/projects/${PROJECT_ID}/tickets`);
    expect(paths).toContain(`/api/projects/${PROJECT_ID}/pending`);
    expect(paths).not.toContain(`/api/projects/${PROJECT_ID}/dashboard`);
  });
});

describe("the Admission filter", () => {
  it("sits last, in a group of its own, carrying the count of issues that are not tickets yet", async () => {
    const root = await page([WAITING, MISCONFIGURED]);
    const labels = [...(statusSelect(root)?.options ?? [])].map((each) =>
      (each.textContent ?? "").replace(/ \(\d+\)$/, ""),
    );
    expect(labels).toEqual([
      "All",
      "Running",
      "Waiting at gate",
      "Stopped",
      "Done",
      "Exited",
      "Admission",
    ]);
    expect(filter(root, "Admission")?.textContent).toBe("Admission (2)");
    // The group is what says these rows are a different kind of thing from the statuses.
    expect(filter(root, "Admission")?.parentElement?.tagName).toBe("OPTGROUP");
  });

  it("swaps the ticket columns for the ones an un-admitted issue has", async () => {
    const root = await page([WAITING]);
    expect(columns(root)).toEqual([
      "Issue",
      "Process",
      "Step",
      "Status",
      "Owner",
      "Runs",
      "Tokens",
      "Cost",
      "Updated",
    ]);

    await pick(root, "Admission");
    await settle();
    expect(columns(root)).toEqual(["Issue", "Process", "Awaiting"]);
  });

  it("names the conditions an issue is waiting on in words, not as rule code", async () => {
    const root = await page([
      { ...WAITING, needs: ["the factorial:new label on the issue", "an assignee on the issue"] },
    ]);
    await pick(root, "Admission");
    await settle();

    expect(root.textContent).toContain(
      "Waiting for: the factorial:new label on the issue; an assignee on the issue",
    );
    expect(root.textContent).not.toContain("ticket_label(");
  });

  it("names the rules an issue is waiting on when the API words none", async () => {
    const root = await page([WAITING]);
    await pick(root, "Admission");
    await settle();

    expect(root.textContent).toContain('ticket_label("factorial:new") = false');
    expect(root.textContent).toContain("ticket_has_assignee() = false");
    expect(root.textContent).toContain("#41");
    // Its issue is on GitHub; there is no ticket page to send a reader to.
    const link = byText(root, "a", "#41");
    expect(link?.getAttribute("href")).toBe(
      "https://github.com/northwind/harbor-invoices/issues/41",
    );
  });

  it("links an issue in the repository its ticket source reads issues from", async () => {
    const root = await page(
      [WAITING],
      project({
        ticket_source: {
          id: 1,
          project_id: PROJECT_ID,
          kind: "github",
          external_ref: "northwind/support-desk",
          webhook_secret_ref: null,
          last_polled_at: null,
          created_at: "2026-07-02T14:18:03Z",
        },
      }),
    );
    await pick(root, "Admission");
    await settle();

    expect(byText(root, "a", "#41")?.getAttribute("href")).toBe(
      "https://github.com/northwind/support-desk/issues/41",
    );
  });

  it("reports a misconfigured issue on its own row rather than in a banner above them", async () => {
    const root = await page([WAITING, MISCONFIGURED]);
    // The banner used to be on the page whatever filter was up.
    expect(root.textContent).not.toContain("no default process is set");

    await pick(root, "Admission");
    await settle();
    const row = [...root.querySelectorAll("tr")].find((each) => each.textContent?.includes("#38"));
    expect(row?.textContent).toContain("Can't be admitted");
    expect(row?.textContent).toContain("no default process is set");
  });

  it("says nothing about what is in flight to a reader who did not ask", async () => {
    const root = await page([]);
    expect(root.textContent).not.toContain("Nothing in flight");
    expect(root.textContent).not.toContain("Waiting to be admitted");
  });
});
