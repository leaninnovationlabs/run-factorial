/**
 * DataTable and Pagination, after ADS's dynamic table.
 *
 * A table that says what it holds (a label, a count, "1–25 of 312"), keeps its head while it
 * reloads, tells "nothing yet" from "nothing matches", and sits in its own focusable scroll region
 * so a narrow window scrolls the table rather than the page.
 *
 * The column heads are a band of their own — a filled head, small tracked capitals — because a
 * table mostly sits under a panel's header, and two rows of short bold words on the same white
 * read as one header twice: the panel's title and actions, then the columns.
 *
 * A table given `stack` draws each row as a stacked item on a phone instead: its title column
 * first, one fact at its right, the rest under it as a wrapping line of named facts — a table of
 * nine columns there is a strip a reader pans across to find the one row they wanted. The
 * columns it names are what a phone keeps; the sort the heads offer moves to a select above.
 * Without `stack` a phone gets the table in its own scroll region, as any narrow window does.
 *
 * Polling must not reshuffle what a reader is looking at. Rows arrive in the API's stable order;
 * a sort the reader chose is applied once, when they choose it, and a poll that changes the
 * sorted figure moves nothing until they sort again (`useStableOrder`).
 */

import { type ReactNode, useRef } from "react";
import { useRowLink } from "../rowLink";
import { Button, IconButton } from "./Button";
import { cx } from "./cx";
import { Icon } from "./icons";
import { useIsPhone } from "./media";
import { SkeletonRows } from "./states";
import { Select } from "./TextField";

export type SortOrder = "asc" | "desc";

export type Column<K extends string = string> = {
  key: K;
  content: ReactNode;
  isSortable?: boolean;
  /** A CSS width for the column, when its content should not decide it. */
  width?: string;
  /** `end` right-aligns the column and sets its figures in tabular numerals. */
  align?: "start" | "end";
};

/** Which columns a phone draws, and where: see the header. */
export type Stack<K extends string> = { title: K; aside?: K; meta?: K[] };

export function DataTable<T, K extends string = string>({
  label,
  head,
  rows,
  rowKey,
  cells,
  rowHref,
  isLoading = false,
  emptyView,
  noResultsView,
  isFiltered = false,
  sortKey,
  sortOrder = "asc",
  onSort,
  rowsPerPage,
  page = 1,
  onSetPage,
  isHeadSticky = false,
  stack,
}: {
  /** What the table lists; its accessible name. */
  label: string;
  head: Column<K>[];
  rows: T[] | undefined;
  rowKey: (row: T) => string | number;
  cells: (row: T) => Record<K, ReactNode>;
  /** Where a click on the row's whitespace goes: the row's own link, repeated for a mouse. */
  rowHref?: (row: T) => string;
  isLoading?: boolean;
  /** Nothing exists yet. */
  emptyView: ReactNode;
  /** Something exists, but a filter matches none of it. */
  noResultsView?: ReactNode;
  isFiltered?: boolean;
  sortKey?: K;
  sortOrder?: SortOrder;
  onSort?: (key: K, order: SortOrder) => void;
  rowsPerPage?: number;
  /** One-based. */
  page?: number;
  onSetPage?: (page: number) => void;
  isHeadSticky?: boolean;
  stack?: Stack<K>;
}) {
  const phone = useIsPhone();
  const all = rows ?? [];
  const pages = rowsPerPage ? Math.max(1, Math.ceil(all.length / rowsPerPage)) : 1;
  const current = Math.min(Math.max(page, 1), pages);
  const shown = rowsPerPage ? all.slice((current - 1) * rowsPerPage, current * rowsPerPage) : all;
  const from = all.length === 0 ? 0 : rowsPerPage ? (current - 1) * rowsPerPage + 1 : 1;
  const to = rowsPerPage ? Math.min(current * rowsPerPage, all.length) : all.length;

  if (rows === undefined && isLoading)
    return <SkeletonRows rows={4} columns={Math.min(head.length, 5)} />;

  const empty = isFiltered && noResultsView ? noResultsView : emptyView;
  const stacked = phone && stack;

  return (
    <div>
      {stacked ? (
        <>
          {onSort && (
            <StackSort head={head} sortKey={sortKey} sortOrder={sortOrder} onSort={onSort} />
          )}
          {shown.length === 0 ? (
            empty
          ) : (
            <ul
              aria-label={label}
              aria-busy={isLoading || undefined}
              className={cx(isLoading && "opacity-loading")}
            >
              {shown.map((row) => (
                <StackedRow
                  key={rowKey(row)}
                  head={head}
                  stack={stack}
                  values={cells(row)}
                  href={rowHref?.(row)}
                />
              ))}
            </ul>
          )}
        </>
      ) : (
        // biome-ignore lint/a11y/useSemanticElements: the scroll box, named; the table is the content
        <div
          role="region"
          aria-label={label}
          // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrolling region must be reachable by keyboard
          tabIndex={0}
          // `relative`, so the visually hidden text in the cells is placed — and clipped — in here.
          // Positioned against `main`, it sat where a wide table's cells are and widened the page.
          className="focus-inset relative overflow-x-auto"
        >
          <table className="w-full border-collapse text-body">
            <caption className="sr-only">
              {label}
              {all.length > 0 && `, ${all.length} ${all.length === 1 ? "row" : "rows"}`}
            </caption>
            <thead className={cx("bg-table-head", isHeadSticky && "sticky top-0 z-card")}>
              <tr className="border-b border-default">
                {head.map((column) => {
                  const active = sortKey === column.key;
                  return (
                    <th
                      key={column.key}
                      scope="col"
                      style={column.width ? { width: column.width } : undefined}
                      aria-sort={
                        active
                          ? sortOrder === "asc"
                            ? "ascending"
                            : "descending"
                          : column.isSortable
                            ? "none"
                            : undefined
                      }
                      className={cx(
                        "px-3 py-2 text-body-sm font-semibold whitespace-nowrap text-subtle",
                        column.align === "end" ? "text-right" : "text-left",
                      )}
                    >
                      {column.isSortable && onSort ? (
                        <button
                          type="button"
                          onClick={() =>
                            onSort(column.key, active && sortOrder === "asc" ? "desc" : "asc")
                          }
                          className="inline-flex items-center gap-1 rounded-sm hover:text-default"
                        >
                          {column.content}
                          <Icon
                            glyph={active && sortOrder === "desc" ? "sort-down" : "sort-up"}
                            size="small"
                            className={active ? "text-icon" : "text-icon-subtlest"}
                          />
                        </button>
                      ) : (
                        column.content
                      )}
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody
              className={cx(isLoading && "opacity-loading")}
              aria-busy={isLoading || undefined}
            >
              {shown.length === 0 ? (
                <tr>
                  <td colSpan={head.length}>{empty}</td>
                </tr>
              ) : (
                shown.map((row) => (
                  <Row key={rowKey(row)} head={head} values={cells(row)} href={rowHref?.(row)} />
                ))
              )}
            </tbody>
          </table>
        </div>
      )}
      {rowsPerPage && all.length > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-default px-3 py-2">
          <p className="text-body-sm text-subtle tabular-nums" aria-live="polite">
            {from} to {to} of {all.length}
          </p>
          <Pagination
            label={`${label} pages`}
            pages={pages}
            selectedIndex={current - 1}
            onChange={(index) => onSetPage?.(index + 1)}
          />
        </div>
      )}
    </div>
  );
}

function Row<K extends string>({
  head,
  values,
  href,
}: {
  head: Column<K>[];
  values: Record<K, ReactNode>;
  href?: string;
}) {
  const link = useRowLink(href ?? "");
  return (
    <tr
      onClick={href ? link.onClick : undefined}
      className={cx(
        "border-b border-default transition-colors duration-50 last:border-b-0 hover:bg-neutral-subtle-hovered",
        href && link.className,
      )}
    >
      {head.map((column) => (
        <td
          key={column.key}
          className={cx(
            "px-3 py-2 align-middle",
            column.align === "end" && "text-right tabular-nums",
          )}
        >
          {values[column.key]}
        </td>
      ))}
    </tr>
  );
}

/** One row on a phone: the title with its aside, then the meta columns, each named by its head. */
function StackedRow<K extends string>({
  head,
  stack,
  values,
  href,
}: {
  head: Column<K>[];
  stack: Stack<K>;
  values: Record<K, ReactNode>;
  href?: string;
}) {
  const link = useRowLink(href ?? "");
  const names = new Map(head.map((column) => [column.key, column.content]));
  return (
    // biome-ignore lint/a11y/useKeyWithClickEvents: the title's own link is the keyboard's way in; this repeats it for a thumb
    <li
      onClick={href ? link.onClick : undefined}
      className={cx(
        "space-y-2 border-b border-default px-4 py-3 transition-colors duration-50 last:border-b-0 hover:bg-neutral-subtle-hovered",
        href && link.className,
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">{values[stack.title]}</div>
        {stack.aside && <div className="shrink-0">{values[stack.aside]}</div>}
      </div>
      {stack.meta && stack.meta.length > 0 && (
        <dl className="flex flex-wrap items-center gap-x-4 gap-y-1 text-body-sm">
          {stack.meta.map((key) => (
            <div key={key} className="flex min-w-0 items-center gap-1.5">
              <dt className="text-subtle">{names.get(key)}</dt>
              <dd className="min-w-0 tabular-nums">{values[key]}</dd>
            </div>
          ))}
        </dl>
      )}
    </li>
  );
}

/** The heads' sort, for a phone that draws no heads: the column, then the direction. */
function StackSort<K extends string>({
  head,
  sortKey,
  sortOrder,
  onSort,
}: {
  head: Column<K>[];
  sortKey?: K;
  sortOrder: SortOrder;
  onSort: (key: K, order: SortOrder) => void;
}) {
  const sortable = head.filter((column) => column.isSortable);
  if (sortable.length === 0) return null;
  return (
    <div className="flex items-center justify-end gap-2 border-b border-default px-4 py-2">
      <Select
        aria-label="Sort by"
        spacing="compact"
        width="medium"
        options={[
          // No sort chosen keeps the rows in the order they came in, which the select must name.
          ...(sortKey === undefined ? [{ value: "", label: "Default order" }] : []),
          ...sortable.map((column) => ({
            value: column.key,
            label: typeof column.content === "string" ? column.content : column.key,
          })),
        ]}
        value={sortKey ?? ""}
        onChange={(event) => {
          const key = event.target.value as K;
          if (key) onSort(key, sortOrder);
        }}
      />
      {sortKey !== undefined && (
        <IconButton
          icon={sortOrder === "asc" ? "sort-up" : "sort-down"}
          label={sortOrder === "asc" ? "Sort descending" : "Sort ascending"}
          appearance="default"
          spacing="compact"
          onClick={() => onSort(sortKey, sortOrder === "asc" ? "desc" : "asc")}
        />
      )}
    </div>
  );
}

/**
 * `rows` in the order a sort put them in when it was chosen. A new `signature` (another column,
 * another direction) sorts again; the same signature keeps the ranks from last time, so a poll that
 * changes the figure being sorted on moves nothing. Rows that arrive later go to the end, in the
 * API's order.
 */
export function useStableOrder<T>(
  rows: T[] | undefined,
  rowKey: (row: T) => string | number,
  compare: ((a: T, b: T) => number) | null,
  signature: string,
): T[] | undefined {
  const ranks = useRef<{ signature: string; order: Map<string | number, number> } | null>(null);
  if (!rows || !compare) return rows;
  if (ranks.current?.signature !== signature) {
    const sorted = [...rows].sort(compare);
    ranks.current = { signature, order: new Map(sorted.map((row, index) => [rowKey(row), index])) };
  }
  const order = ranks.current.order;
  const rank = (row: T) => order.get(rowKey(row)) ?? Number.MAX_SAFE_INTEGER;
  return [...rows].sort((a, b) => rank(a) - rank(b));
}

/** The page numbers to show: the first, the last, and the current one with its neighbours. */
export function pageWindow(pages: number, selected: number): (number | "gap")[] {
  if (pages <= 7) return Array.from({ length: pages }, (_, index) => index);
  const shown = new Set([0, pages - 1, selected - 1, selected, selected + 1]);
  const out: (number | "gap")[] = [];
  for (let index = 0; index < pages; index += 1) {
    if (shown.has(index)) out.push(index);
    else if (out.at(-1) !== "gap") out.push("gap");
  }
  return out;
}

export function Pagination({
  pages,
  selectedIndex,
  onChange,
  label = "Pages",
}: {
  pages: number;
  selectedIndex: number;
  onChange: (index: number) => void;
  label?: string;
}) {
  if (pages <= 1) return null;
  return (
    <nav aria-label={label} className="flex items-center gap-1">
      <IconButton
        icon="chevron-left"
        label="Previous page"
        spacing="compact"
        isDisabled={selectedIndex === 0}
        onClick={() => onChange(selectedIndex - 1)}
      />
      {pageWindow(pages, selectedIndex).map((entry, index) =>
        entry === "gap" ? (
          // biome-ignore lint/suspicious/noArrayIndexKey: a gap has no identity but its place
          <span key={`gap-${index}`} aria-hidden="true" className="px-1 text-subtlest">
            …
          </span>
        ) : (
          <Button
            key={entry}
            appearance="ghost"
            spacing="compact"
            isSelected={entry === selectedIndex}
            aria-current={entry === selectedIndex ? "page" : undefined}
            aria-label={`Page ${entry + 1}`}
            onClick={() => onChange(entry)}
          >
            {entry + 1}
          </Button>
        ),
      )}
      <IconButton
        icon="chevron-right"
        label="Next page"
        spacing="compact"
        isDisabled={selectedIndex >= pages - 1}
        onClick={() => onChange(selectedIndex + 1)}
      />
    </nav>
  );
}
