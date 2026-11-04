/**
 * What the issue list does to its rows before drawing them: name, search, sort, and cut into pages
 * that exactly fill the space the list has.
 *
 * All of it is pure except the hook at the bottom, a thin wrapper over a function that is —
 * which is what makes any of this testable in a `node` environment with no DOM. The arithmetic
 * is the part that can be wrong; the observer only feeds it a number.
 */

import { useEffect, useRef, useState } from "react";
import {
  formatMetric,
  type IssueNode,
  LANE_LABELS,
  LANES,
  laneOf,
  type Metric,
  metricValue,
} from "./model";

// --- naming ---------------------------------------------------------------------------------

/**
 * The issue as people name it: `#123`. The API's key is the bare number (admission stores
 * `issue.number`); a key already carrying a repo is cut to its number, the repo being the row's
 * project suffix already. Anything else is shown whole rather than mangled.
 */
export function issueNumber(externalKey: string): string {
  if (/^\d+$/.test(externalKey)) return `#${externalKey}`;
  const hash = externalKey.lastIndexOf("#");
  return hash === -1 ? externalKey : externalKey.slice(hash);
}

/** What the row cannot fit: the whole title, the process and step, the lane, and the figure. */
export function rowFacts(node: IssueNode, metric: Metric | null): string {
  const spend = metric === null ? "" : ` · ${formatMetric(node, metric)}`;
  const where = `${node.process ? `${node.process}/` : ""}${node.step} · ${LANE_LABELS[laneOf(node)]}`;
  return `${node.title ? `${node.title}\n` : ""}${where}${spend}`;
}

// --- search ---------------------------------------------------------------------------------

/**
 * Every token has to appear in the row's issue number or its title, and order does not matter.
 *
 * A leading `#` is dropped so `#118` and `118` are the same query. A ticket with no stored title
 * is still searchable by number, which is all it could ever be matched on.
 */
export function matchesQuery(node: IssueNode, query: string): boolean {
  const tokens = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return true;
  const haystack = `${issueNumber(node.key)} ${node.title ?? ""}`.toLowerCase();
  return tokens.every((token) => haystack.includes(token.replace(/^#/, "")));
}

export function searchIssues(nodes: IssueNode[], query: string): IssueNode[] {
  return query.trim() === "" ? nodes : nodes.filter((node) => matchesQuery(node, query));
}

// --- sort -----------------------------------------------------------------------------------

export type SortKey = "status" | "number" | "project" | "metric";

export interface Sort {
  key: SortKey;
  dir: "asc" | "desc";
}

/**
 * The direction each key means when it is first chosen.
 *
 * Every key has an obvious "interesting end" and it is not the same one: a number is a clock, so
 * newest first; a name is an index, so A first; a metric is a complaint, so worst first. Picking
 * a key and then having to fix the direction is one press too many.
 */
export function naturalDir(key: SortKey): Sort["dir"] {
  return key === "number" || key === "metric" ? "desc" : "asc";
}

/**
 * What the list sorts by when nobody has said.
 *
 * Newest issue first normally, because an issue number is a clock and the recent end is where
 * the news is. With a metric on it is that metric worst-first instead: the reason to turn a
 * metric on is to find what is wrong, and a list that answered in issue order would make the
 * reader do the search the sort was there to do.
 */
export function defaultSort(metric: Metric | null): Sort {
  return metric === null ? { key: "number", dir: "desc" } : { key: "metric", dir: "desc" };
}

/** How far down the pipeline a ticket is — the order the board's own columns are in. */
function statusOf(node: IssueNode): number {
  return LANES.indexOf(laneOf(node));
}

/** The issue's number, for ordering. Falls back to the ticket id for a key with no `#`. */
function numberOf(node: IssueNode): number {
  const parsed = Number(issueNumber(node.key).replace(/^#/, ""));
  return Number.isFinite(parsed) ? parsed : node.id;
}

/**
 * Sorted, on a copy.
 *
 * `nameOf` is how a project sort orders by the word the row actually shows rather than by an id
 * the reader never sees. It is optional because the other three keys do not need it.
 *
 * Every comparison ends in a tie-break on ticket id, for the same reason the board's does: two
 * rows the sort cannot separate must not swap places when something unrelated re-renders.
 */
export function sortIssues(
  nodes: IssueNode[],
  sort: Sort,
  metric: Metric | null,
  nameOf: (projectId: number) => string = String,
): IssueNode[] {
  const sign = sort.dir === "asc" ? 1 : -1;
  return nodes.slice().sort((a, b) => {
    let by: number;
    switch (sort.key) {
      case "status":
        by = statusOf(a) - statusOf(b);
        break;
      case "project":
        by = nameOf(a.projectId).localeCompare(nameOf(b.projectId));
        break;
      case "metric":
        // A metric sort with no metric on has nothing to rank by, so it falls through to the
        // number rather than pretending every row is equal. Reachable: an explicit "worst first"
        // survives the reader turning the metric off.
        by =
          metric === null
            ? numberOf(a) - numberOf(b)
            : metricValue(a, metric) - metricValue(b, metric);
        break;
      default:
        by = numberOf(a) - numberOf(b);
    }
    return by !== 0 ? sign * by : a.id - b.id;
  });
}

// --- pages ----------------------------------------------------------------------------------

/**
 * One row's height in px. Must match `--fb-row` in the stylesheet — `tokens.test.ts` holds the
 * two together, because a drift here does not break anything visibly: it just quietly pages one
 * row short, or one row into a scrollbar the list is not supposed to have.
 */
export const ROW_PX = 26;

/** One row on a phone, where the list stands without the board and a row is pressed by a thumb:
 *  `--fb-row` under the phone query in `factory-board.css`, held to this by `tokens.test.ts`. */
export const ROW_PX_TOUCH = 44;

/** Rows to assume before anything has been measured — one server render, one frame. */
export const ROWS_FALLBACK = 12;

/** How many whole rows fit. At least one, so an unmeasurably short list still shows something. */
export function pageSizeFor(height: number, rowPx: number = ROW_PX): number {
  return Math.max(1, Math.floor(height / rowPx));
}

export interface Page {
  rows: IssueNode[];
  /** The page actually shown — clamped, so a shrinking result set cannot strand the reader. */
  page: number;
  pages: number;
  /** 1-based, inclusive, for the "x–y of z" line. Both 0 when there is nothing. */
  from: number;
  to: number;
  total: number;
}

export function pageOf(nodes: IssueNode[], page: number, size: number): Page {
  const pages = Math.max(1, Math.ceil(nodes.length / size));
  const shown = Math.min(Math.max(0, page), pages - 1);
  const start = shown * size;
  const rows = nodes.slice(start, start + size);
  return {
    rows,
    page: shown,
    pages,
    from: nodes.length === 0 ? 0 : start + 1,
    to: start + rows.length,
    total: nodes.length,
  };
}

/**
 * Which page a ticket is on, or null if this list does not hold it.
 *
 * Null rather than 0 for the miss, because the two mean opposite things: a ticket the list is not
 * showing — searched out, or in another project — must leave the reader on the page they were
 * reading, not send them to the top of a list that does not contain what they clicked.
 */
export function pageOfNode(nodes: IssueNode[], id: number, size: number): number | null {
  const at = nodes.findIndex((node) => node.id === id);
  return at === -1 ? null : Math.floor(at / Math.max(1, size));
}

// --- measurement ------------------------------------------------------------------------------

/**
 * How many rows fit in an element, watched.
 *
 * A `ResizeObserver` rather than a resize listener, because the box this watches changes for
 * reasons the window does not know about — the header above it growing a second line when a
 * focus is named, say.
 */
export function useRowCapacity<T extends HTMLElement>(rowPx: number = ROW_PX) {
  const ref = useRef<T>(null);
  const [rows, setRows] = useState(ROWS_FALLBACK);

  useEffect(() => {
    const node = ref.current;
    if (node === null || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setRows(pageSizeFor(entry.contentRect.height, rowPx));
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [rowPx]);

  return { ref, rows };
}
