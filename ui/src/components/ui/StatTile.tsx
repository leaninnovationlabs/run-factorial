/**
 * A figure the page leads with: a label, one number, and a caption that gives it its context
 * (`$66.17` · "lifetime · 17 tickets"). `Metric` is the row-level figure; this is the hero one,
 * at `text-metric-lg`, in the house sans with proportional digits — a standalone number is read
 * as a shape, and tabular digits make `121` sit loose at this size.
 *
 * `value` is text already formatted by the caller; nothing is computed here, and no value draws
 * as a dash rather than as a zero, because "nothing yet" and "0" are different claims.
 */

import type { ReactNode } from "react";
import { NO_VALUE } from "../../format";

export function StatTile({
  label,
  value,
  change,
  caption,
  aside,
}: {
  label: string;
  value: string | null | undefined;
  /** Under the figure: how it moved against an earlier period. */
  change?: ReactNode;
  caption?: ReactNode;
  /** Beside the figure: an estimate's explainer, a sparkline, or nothing. */
  aside?: ReactNode;
}) {
  return (
    <div
      data-stat
      className="flex min-w-0 flex-col gap-1 rounded-lg border border-default bg-surface px-4 py-3"
    >
      <span data-stat-label className="text-body-sm text-subtle">
        {label}
      </span>
      <span className="flex flex-wrap items-baseline gap-x-2">
        <span data-stat-value className="text-metric-md text-default sm:text-metric-lg">
          {value ?? NO_VALUE}
        </span>
        {aside}
      </span>
      {change}
      {caption && (
        <span data-stat-caption className="text-body-sm text-subtlest">
          {caption}
        </span>
      )}
    </div>
  );
}
