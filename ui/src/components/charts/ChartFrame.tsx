/**
 * What every chart shares: the frame (a title, a legend once there are two series, the picture, a
 * line naming where the numbers came from), the series colours, and the axes.
 *
 * The colours are the design system's categorical chart tokens, which `tokens.css` defines for
 * both themes, in a fixed order whose neighbours stay apart under colour blindness, so a series
 * keeps its colour wherever it is drawn. A chart has at most six series. Words never wear a series
 * colour: the swatch beside a name carries it.
 *
 * A plot is drawn in a fixed coordinate box and scaled to its width, so the axes are laid out once
 * here for both kinds of chart.
 */

import type { ReactNode } from "react";

export const SERIES_COLOURS = [
  "var(--ds-chart-blue-bold)",
  "var(--ds-chart-magenta-bold)",
  "var(--ds-chart-orange-bold)",
  "var(--ds-chart-teal-bold)",
  "var(--ds-chart-lime-bold)",
  "var(--ds-chart-purple-bold)",
] as const;

export function seriesColour(index: number): string {
  return SERIES_COLOURS[index] ?? "var(--ds-chart-neutral)";
}

export type ChartSeries = { name: string; values: number[] };

export const PLOT = { width: 640, height: 240, left: 64, right: 16, top: 28, bottom: 28 };
const INNER_HEIGHT = PLOT.height - PLOT.top - PLOT.bottom;
const MOST_X_LABELS = 8;

/** Round values from zero (or below it) up to cover `high`: 0, 1, 2… or 0, 250, 500… */
export function ticks(low: number, high: number, count = 4): number[] {
  const floor = Math.min(0, low);
  const ceiling = Math.max(0, high);
  if (ceiling === floor) return [0, 1];
  const rough = (ceiling - floor) / count;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const step = ([1, 2, 2.5, 5, 10].find((m) => m * magnitude >= rough) ?? 10) * magnitude;
  const values: number[] = [];
  for (let at = Math.floor(floor / step) * step; ; at += step) {
    const value = Number(at.toPrecision(12));
    values.push(value);
    if (value >= ceiling) return values;
  }
}

/** The vertical scale for `series`: where a value sits, and the ticks that frame it. */
export function valueScale(series: ChartSeries[]) {
  const values = series.flatMap((line) => line.values);
  const marks = ticks(Math.min(...values), Math.max(...values));
  const low = marks[0] ?? 0;
  const high = marks.at(-1) ?? 1;
  const y = (value: number) => PLOT.top + ((high - value) / (high - low)) * INNER_HEIGHT;
  return { ticks: marks, y };
}

/** The gridlines with their values, the unit above them, and the labels along the bottom. Only
 *  every so many labels are written once there are too many to read. */
export function Axes({
  x,
  xAt,
  ticks: marks,
  y,
  unit,
  format,
}: {
  x: string[];
  xAt: (index: number) => number;
  ticks: number[];
  y: (value: number) => number;
  unit: string;
  format: (value: number) => string;
}) {
  const every = Math.ceil(x.length / MOST_X_LABELS);
  return (
    <g className="fill-current text-body-sm text-subtle">
      <text x={0} y={12}>
        {unit}
      </text>
      {marks.map((mark) => (
        <g key={mark}>
          <line
            x1={PLOT.left}
            x2={PLOT.width - PLOT.right}
            y1={y(mark)}
            y2={y(mark)}
            strokeWidth={1}
            style={{ stroke: "var(--ds-border)" }}
          />
          <text x={PLOT.left - 8} y={y(mark)} dy="0.32em" textAnchor="end">
            {format(mark)}
          </text>
        </g>
      ))}
      {x.map((label, index) =>
        index % every === 0 ? (
          // biome-ignore lint/suspicious/noArrayIndexKey: two labels may read the same
          <text key={index} x={xAt(index)} y={PLOT.height - 8} textAnchor="middle">
            {label}
          </text>
        ) : null,
      )}
    </g>
  );
}

export function ChartFrame({
  title,
  series,
  mark,
  actions,
  footer,
  children,
}: {
  title: string;
  series: ChartSeries[];
  /** How the legend keys a series: a short stroke for a line, a block for a bar. */
  mark: "line" | "bar";
  actions?: ReactNode;
  footer?: ReactNode;
  children: ReactNode;
}) {
  return (
    <figure className="space-y-2 rounded-lg border border-default bg-surface p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <figcaption className="text-heading-xs text-default">{title}</figcaption>
        {actions}
      </div>
      {series.length > 1 && (
        <ul data-legend className="flex flex-wrap gap-x-4 gap-y-1 text-body-sm text-subtle">
          {series.map((line, index) => (
            <li key={line.name} className="inline-flex items-center gap-1.5">
              <span
                aria-hidden="true"
                className={mark === "line" ? "h-0.5 w-3 rounded-full" : "size-2.5 rounded-xs"}
                style={{ backgroundColor: seriesColour(index) }}
              />
              {line.name}
            </li>
          ))}
        </ul>
      )}
      {children}
      {footer && <p className="text-body-sm text-subtle">{footer}</p>}
    </figure>
  );
}
