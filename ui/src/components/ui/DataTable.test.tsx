// @vitest-environment happy-dom
/**
 * The table's promises: it says how it is sorted, sorts on a press, pages with one pager, tells
 * "nothing yet" from "nothing matches" — and a poll never reshuffles what a reader is looking at.
 */

import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { byText, click, type Mounted, mount } from "../../testDom";
import { type Column, DataTable, pageWindow, useStableOrder } from "./DataTable";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
});

type Ticket = { id: number; key: string; cost: number };

const HEAD: Column<"key" | "cost" | "step">[] = [
  { key: "key", content: "Issue", isSortable: true },
  { key: "cost", content: "Cost", isSortable: true, align: "end" },
  { key: "step", content: "Step" },
];

const TICKETS: Ticket[] = [1, 2, 3, 4, 5].map((id) => ({ id, key: `#${id}`, cost: id * 10 }));

function table(
  overrides: Partial<Parameters<typeof DataTable<Ticket, "key" | "cost" | "step">>[0]> = {},
) {
  return (
    <MemoryRouter>
      <DataTable<Ticket, "key" | "cost" | "step">
        label="Tickets"
        head={HEAD}
        rows={TICKETS}
        rowKey={(row) => row.id}
        cells={(row) => ({ key: row.key, cost: row.cost, step: "build" })}
        emptyView={<p>No tickets yet</p>}
        noResultsView={<p>No tickets match</p>}
        {...overrides}
      />
    </MemoryRouter>
  );
}

const header = (name: string) =>
  [...document.querySelectorAll("th")].find((th) => th.textContent === name) as HTMLElement;

describe("DataTable", () => {
  it("says which column it is sorted on, and which way, and which columns can sort", async () => {
    view = await mount(table({ sortKey: "cost", sortOrder: "desc", onSort: vi.fn() }));
    expect(header("Cost").getAttribute("aria-sort")).toBe("descending");
    expect(header("Issue").getAttribute("aria-sort")).toBe("none");
    expect(header("Step").hasAttribute("aria-sort")).toBe(false);
  });

  it("sorts on a press: a new column ascending, the sorted column the other way", async () => {
    const onSort = vi.fn();
    view = await mount(table({ sortKey: "cost", sortOrder: "asc", onSort }));
    await click(header("Cost").querySelector("button"));
    expect(onSort).toHaveBeenLastCalledWith("cost", "desc");
    await click(header("Issue").querySelector("button"));
    expect(onSort).toHaveBeenLastCalledWith("key", "asc");
  });

  it("names itself and counts its rows for a screen reader", async () => {
    view = await mount(table());
    expect(document.querySelector('[role="region"]')?.getAttribute("aria-label")).toBe("Tickets");
    expect(document.querySelector("caption")?.textContent).toBe("Tickets, 5 rows");
  });

  it("positions its scroll box, so hidden text in a wide table cannot widen the page", async () => {
    // Visually hidden text is absolutely positioned. With no positioned ancestor inside the box it
    // is placed against `main`, where the table's far cells are, and `main` scrolls sideways on a
    // phone. happy-dom has no layout, so the mechanism is what can be held here.
    view = await mount(table());
    expect(document.querySelector('[role="region"]')?.classList).toContain("relative");
  });

  it("tells nothing yet from nothing matching", async () => {
    view = await mount(table({ rows: [] }));
    expect(document.body.textContent).toContain("No tickets yet");
    await view.rerender(table({ rows: [], isFiltered: true }));
    expect(document.body.textContent).toContain("No tickets match");
  });

  it("pages with one pager: the range, the current page, and ends that cannot be passed", async () => {
    const onSetPage = vi.fn();
    view = await mount(table({ rowsPerPage: 2, page: 1, onSetPage }));
    expect(document.querySelectorAll("tbody tr")).toHaveLength(2);
    expect(document.body.textContent).toContain("1 to 2 of 5");
    const nav = document.querySelector('nav[aria-label="Tickets pages"]') as HTMLElement;
    expect(byText(nav, "button", "1")?.getAttribute("aria-current")).toBe("page");
    expect((nav.querySelector('[aria-label="Previous page"]') as HTMLButtonElement).disabled).toBe(
      true,
    );
    await click(byText(nav, "button", "3"));
    expect(onSetPage).toHaveBeenCalledWith(3);

    await view.rerender(table({ rowsPerPage: 2, page: 3, onSetPage }));
    expect(document.body.textContent).toContain("5 to 5 of 5");
    const last = document.querySelector('nav[aria-label="Tickets pages"]') as HTMLElement;
    expect((last.querySelector('[aria-label="Next page"]') as HTMLButtonElement).disabled).toBe(
      true,
    );
  });

  it("keeps its head while it reloads, marking the body busy rather than blanking it", async () => {
    view = await mount(table({ isLoading: true }));
    expect(header("Issue")).toBeDefined();
    expect(document.querySelector("tbody")?.getAttribute("aria-busy")).toBe("true");
  });
});

describe("pageWindow", () => {
  it("shows every page when there are few", () => {
    expect(pageWindow(5, 2)).toEqual([0, 1, 2, 3, 4]);
  });

  it("keeps the ends and the neighbours of the current page, and closes the rest into gaps", () => {
    expect(pageWindow(12, 5)).toEqual([0, "gap", 4, 5, 6, "gap", 11]);
    expect(pageWindow(12, 0)).toEqual([0, 1, "gap", 11]);
    expect(pageWindow(12, 11)).toEqual([0, "gap", 10, 11]);
  });
});

function Ordered({ rows, signature }: { rows: Ticket[]; signature: string }) {
  const ordered = useStableOrder(
    rows,
    (row) => row.id,
    (a, b) => (signature === "cost:desc" ? b.cost - a.cost : a.cost - b.cost),
    signature,
  );
  return <p>{ordered?.map((row) => row.key).join(" ")}</p>;
}

describe("useStableOrder", () => {
  const rows: Ticket[] = [
    { id: 1, key: "#1", cost: 30 },
    { id: 2, key: "#2", cost: 10 },
    { id: 3, key: "#3", cost: 20 },
  ];
  const order = () => document.querySelector("p")?.textContent;

  it("sorts once when the sort is chosen, and a poll that changes the figure moves nothing", async () => {
    view = await mount(<Ordered rows={rows} signature="cost:desc" />);
    expect(order()).toBe("#1 #3 #2");
    const polled = rows.map((row) => (row.id === 2 ? { ...row, cost: 99 } : row));
    await view.rerender(<Ordered rows={polled} signature="cost:desc" />);
    expect(order()).toBe("#1 #3 #2");
  });

  it("sorts again when the reader asks for another order", async () => {
    view = await mount(<Ordered rows={rows} signature="cost:desc" />);
    await view.rerender(<Ordered rows={rows} signature="cost:asc" />);
    expect(order()).toBe("#2 #3 #1");
  });

  it("puts a row that arrives later at the end, rather than in the middle of the reading", async () => {
    view = await mount(<Ordered rows={rows} signature="cost:desc" />);
    await view.rerender(
      <Ordered rows={[...rows, { id: 4, key: "#4", cost: 100 }]} signature="cost:desc" />,
    );
    expect(order()).toBe("#1 #3 #2 #4");
  });
});
