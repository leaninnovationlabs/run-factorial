/**
 * The arithmetic the issue list runs before it draws anything.
 *
 * All of it is pure by design — the hooks in `list.ts` only feed these functions a measured
 * number — which is what makes the part that can actually be wrong reachable from a `node`
 * environment with no DOM.
 */

import { describe, expect, it } from "vitest";
import {
  defaultSort,
  matchesQuery,
  naturalDir,
  pageOf,
  pageOfNode,
  pageSizeFor,
  ROW_PX,
  searchIssues,
  sortIssues,
} from "./list";
import type { IssueNode } from "./model";

function node(id: number, over: Partial<IssueNode> = {}): IssueNode {
  return {
    id,
    key: `acme/repo#${id}`,
    title: "Retry the webhook",
    projectId: 1,
    step: "build",
    latestRunStatus: null,
    status: "running",
    runs: 1,
    attempt: 1,
    costUsd: 0,
    minutes: 0,
    tokens: 0,
    ...over,
  };
}

describe("matchesQuery", () => {
  const webhook = node(118);

  it("matches on the issue number", () => {
    expect(matchesQuery(webhook, "118")).toBe(true);
    expect(matchesQuery(webhook, "119")).toBe(false);
  });

  it("matches on words of the title, in any order", () => {
    expect(matchesQuery(webhook, "webhook")).toBe(true);
    expect(matchesQuery(webhook, "WEBHOOK retry")).toBe(true);
    expect(matchesQuery(webhook, "webhook 118")).toBe(true);
    expect(matchesQuery(webhook, "webhook 119")).toBe(false);
    expect(matchesQuery(webhook, "payments")).toBe(false);
  });

  it("still searches by number when the ticket has no stored title", () => {
    const untitled = node(118, { title: null });
    expect(matchesQuery(untitled, "118")).toBe(true);
    expect(matchesQuery(untitled, "webhook")).toBe(false);
  });

  it("ignores case and a leading hash, so #118 and 118 are one query", () => {
    expect(matchesQuery(webhook, "#118")).toBe(true);
    expect(matchesQuery(webhook, "#119")).toBe(false);
  });

  it("matches everything on an empty or blank query", () => {
    expect(matchesQuery(webhook, "")).toBe(true);
    expect(searchIssues([webhook], "   ")).toHaveLength(1);
  });
});

describe("naturalDir", () => {
  it("gives every key the end that is worth looking at first", () => {
    // A number is a clock and a metric is a complaint, so both open at their high end; a name is
    // an index and a status is a pipeline, so both open at their start.
    expect(naturalDir("number")).toBe("desc");
    expect(naturalDir("metric")).toBe("desc");
    expect(naturalDir("project")).toBe("asc");
    expect(naturalDir("status")).toBe("asc");
  });
});

describe("defaultSort", () => {
  it("is newest-first normally, and worst-first once a metric is on", () => {
    // The reason to turn a metric on is to find what is wrong. A list still in issue order would
    // make the reader do by eye the search the sort was there to do.
    expect(defaultSort(null)).toEqual({ key: "number", dir: "desc" });
    expect(defaultSort("cost")).toEqual({ key: "metric", dir: "desc" });
  });
});

describe("sortIssues", () => {
  const nodes = [node(3, { costUsd: 5 }), node(1, { costUsd: 9 }), node(2, { costUsd: 1 })];
  const ids = (sorted: IssueNode[]) => sorted.map((n) => n.id);

  it("orders by issue number both ways", () => {
    expect(ids(sortIssues(nodes, { key: "number", dir: "desc" }, null))).toEqual([3, 2, 1]);
    expect(ids(sortIssues(nodes, { key: "number", dir: "asc" }, null))).toEqual([1, 2, 3]);
  });

  it("orders by the status indicator down the pipeline, not alphabetically", () => {
    // The swatch is what the reader is sorting by, and its meaning is a position in the flow —
    // Admitted before Done. An alphabetical lane sort would put Done second and mean nothing.
    const lanes = [
      node(1, { status: "done", runs: 4, attempt: null }),
      node(2, { status: "running", runs: 0, attempt: null }),
      node(3, { status: "blocked", runs: 2, attempt: 1 }),
    ];
    expect(ids(sortIssues(lanes, { key: "status", dir: "asc" }, null))).toEqual([2, 3, 1]);
    expect(ids(sortIssues(lanes, { key: "status", dir: "desc" }, null))).toEqual([1, 3, 2]);
  });

  it("orders by the project NAME the row shows, not by the id it does not", () => {
    const across = [
      node(1, { projectId: 7 }),
      node(2, { projectId: 3 }),
      node(3, { projectId: 5 }),
    ];
    const names = new Map([
      [7, "alpha"],
      [3, "zulu"],
      [5, "mike"],
    ]);
    const nameOf = (id: number) => names.get(id) ?? "";
    expect(ids(sortIssues(across, { key: "project", dir: "asc" }, null, nameOf))).toEqual([
      1, 3, 2,
    ]);
  });

  it("orders by the metric's severity when one is on", () => {
    expect(ids(sortIssues(nodes, { key: "metric", dir: "desc" }, "cost"))).toEqual([1, 3, 2]);
  });

  it("falls back to the number when a metric sort has no metric", () => {
    // Reachable: an explicit "worst first" survives the reader turning the metric off, and a
    // comparator that returned 0 for every pair would leave the list in an arbitrary order.
    expect(ids(sortIssues(nodes, { key: "metric", dir: "desc" }, null))).toEqual([3, 2, 1]);
  });

  it("does not mutate its input", () => {
    const before = ids(nodes);
    sortIssues(nodes, { key: "number", dir: "asc" }, null);
    expect(ids(nodes)).toEqual(before);
  });

  it("breaks ties on id, so equal rows never swap on an unrelated re-render", () => {
    const flat = [node(9, { costUsd: 2 }), node(4, { costUsd: 2 }), node(7, { costUsd: 2 })];
    expect(ids(sortIssues(flat, { key: "metric", dir: "desc" }, "cost"))).toEqual([4, 7, 9]);
  });
});

describe("pageOf", () => {
  const nodes = Array.from({ length: 25 }, (_, i) => node(i + 1));

  it("cuts the list into pages and counts them from one for the reader", () => {
    const first = pageOf(nodes, 0, 10);
    expect(first.rows).toHaveLength(10);
    expect(first).toMatchObject({ page: 0, pages: 3, from: 1, to: 10, total: 25 });
    expect(pageOf(nodes, 2, 10)).toMatchObject({ from: 21, to: 25, pages: 3 });
  });

  it("clamps a page past the end rather than showing nothing", () => {
    // A search that shrinks the result set must not strand the reader on a blank page nine.
    expect(pageOf(nodes, 99, 10)).toMatchObject({ page: 2, from: 21, to: 25 });
  });

  it("reports an empty list as empty rather than as row zero", () => {
    expect(pageOf([], 0, 10)).toMatchObject({ pages: 1, from: 0, to: 0, total: 0 });
  });
});

describe("the measurements", () => {
  it("fits whole rows only, and never fewer than one", () => {
    expect(pageSizeFor(260, 26)).toBe(10);
    // 11 rows would need 286: the eleventh must not be half-drawn behind a hidden overflow.
    expect(pageSizeFor(285, 26)).toBe(10);
    expect(pageSizeFor(4, 26)).toBe(1);
  });

  it("keeps the row constant a whole number of pixels", () => {
    // It is divided into a measured height. A fractional row would drift a page short on tall
    // screens and not on short ones, which is the worst kind of bug to notice.
    expect(Number.isInteger(ROW_PX)).toBe(true);
  });
});

describe("pageOfNode", () => {
  const many = Array.from({ length: 30 }, (_, index) => node(index + 1));

  it("finds the page a ticket is on, at the size the list is actually showing", () => {
    expect(pageOfNode(many, 1, 12)).toBe(0);
    expect(pageOfNode(many, 12, 12)).toBe(0);
    expect(pageOfNode(many, 13, 12)).toBe(1);
    expect(pageOfNode(many, 30, 12)).toBe(2);
  });

  it("says null for a ticket this list does not hold, rather than page one", () => {
    // A square can be clicked whose ticket the list is not showing — searched out, or in another
    // project. Sending the reader to the top of a list that does not contain what they clicked
    // would be a worse answer than leaving them where they were.
    expect(pageOfNode(many, 999, 12)).toBeNull();
    expect(pageOfNode([], 1, 12)).toBeNull();
  });

  it("survives being asked before the list has measured itself", () => {
    // `body.rows` is zero until the observer has run once, and a page size of zero would divide
    // the reader's place by nothing.
    expect(pageOfNode(many, 13, 0)).toBe(12);
  });
});
