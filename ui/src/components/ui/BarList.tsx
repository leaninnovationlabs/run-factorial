/**
 * Rows of one bar each — a label, a length, a figure — the form for "which of these is biggest".
 *
 * Every bar is a share of the same `max`, so rows compare; a row may be cut into segments, which
 * are shares of that same `max` too, so a segmented row still compares with its neighbours. A
 * value of zero still draws (2px): a row that paints nothing reads as a row that has not loaded.
 *
 * The figure is text in its own column, and `detail` adds a line under a bar. A segmented row's
 * parts are text somewhere too, in `detail` or in a table beside the list, so nothing is only in
 * the hover bubble. Three axis labels under the bars — 0, half, and the max —
 * name values the chart reaches. The label column is wide (12rem) or narrow (4.5rem, a bare key).
 * Below `sm` the label takes its own line above the bar: a column beside it would leave a phone a
 * letter of each title.
 */

import type { ReactNode } from "react";
import { type Look, lookStyle, useDelegatedTip } from "./chart";
import { cx } from "./cx";
import { AnchoredTooltip } from "./Tooltip";

export type BarSegment = {
  key: string;
  look: Look;
  value: number;
  tip?: string;
};

export type BarRow = {
  key: string;
  label: ReactNode;
  /** The whole bar's quantity; with `segments`, their sum. */
  value: number;
  /** What a reader sees for `value`. */
  text: string;
  look?: Look;
  segments?: BarSegment[];
  tip?: string;
  /** A line under the bar, at the bar's width: a segmented row's parts, in words and figures. */
  detail?: ReactNode;
};

const COLUMNS = {
  wide: "grid grid-cols-[1fr_4rem] items-center gap-x-3 sm:grid-cols-[12rem_1fr_5rem]",
  narrow: "grid grid-cols-[1fr_4rem] items-center gap-x-3 sm:grid-cols-[4.5rem_1fr_5rem]",
};

export function BarList({
  label,
  rows,
  max,
  format,
  labelWidth = "wide",
}: {
  /** What the rows are; the list's accessible name. */
  label: string;
  rows: BarRow[];
  /** The value a full-width bar stands for; the largest row when left out. */
  max?: number;
  /** Writes the axis labels; no axis without it. */
  format?: (value: number) => string;
  /** Narrow fits a bare key; wide leaves room for a title beside it. */
  labelWidth?: "wide" | "narrow";
}) {
  const { tip, handlers } = useDelegatedTip();
  const top = max ?? rows.reduce((most, row) => Math.max(most, row.value), 0);
  const share = (value: number) => (top > 0 ? `${(Math.max(0, value) / top) * 100}%` : "0%");
  return (
    <div {...handlers}>
      <ul aria-label={label} className="space-y-3 sm:space-y-1">
        {rows.map((row) => (
          <li key={row.key} data-row={row.key} className={cx(COLUMNS[labelWidth], "min-h-6")}>
            <span className="col-span-2 min-w-0 truncate text-body sm:col-span-1">{row.label}</span>
            <span aria-hidden="true" className="flex gap-0.5" data-tip={row.tip}>
              {row.segments ? (
                row.segments.map((segment) => (
                  <span
                    key={segment.key}
                    data-segment={segment.key}
                    data-chart-look={segment.look}
                    data-tip={segment.tip}
                    style={{ width: share(segment.value), ...lookStyle(segment.look) }}
                    className="h-4 min-w-0.5 bg-[var(--look)] last:rounded-r-sm"
                  />
                ))
              ) : (
                <span
                  data-bar
                  data-chart-look={row.look ?? "muted"}
                  style={{ width: share(row.value), ...lookStyle(row.look ?? "muted") }}
                  className="h-4 min-w-0.5 rounded-r-sm bg-[var(--look)]"
                />
              )}
            </span>
            <span className="text-right text-body tabular-nums">{row.text}</span>
            {row.detail && (
              <span className="col-span-2 text-body-sm text-subtle sm:col-start-2">
                {row.detail}
              </span>
            )}
          </li>
        ))}
      </ul>
      {format && top > 0 && (
        <div aria-hidden="true" className={cx(COLUMNS[labelWidth], "mt-1")}>
          <span className="flex justify-between text-body-sm text-subtlest tabular-nums sm:col-start-2">
            <span>{format(0)}</span>
            <span>{format(top / 2)}</span>
            <span>{format(top)}</span>
          </span>
        </div>
      )}
      {tip !== null && <AnchoredTooltip anchor={tip.anchor} content={tip.text} />}
    </div>
  );
}
