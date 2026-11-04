/**
 * A quantity over time: one 2px line per series through a dot at each value, on round gridlines
 * from zero. Each dot names its series, label and value when the pointer rests on it; the card's
 * table holds the same numbers for every other reader.
 */

import { Axes, type ChartSeries, PLOT, seriesColour, valueScale } from "./ChartFrame";

export function LineChart({
  label,
  x,
  series,
  unit,
  format,
}: {
  /** What the picture shows, read out in place of it. */
  label: string;
  x: string[];
  series: ChartSeries[];
  unit: string;
  format: (value: number) => string;
}) {
  const { ticks, y } = valueScale(series);
  const span = PLOT.width - PLOT.left - PLOT.right;
  const xAt = (index: number) =>
    x.length === 1 ? PLOT.left + span / 2 : PLOT.left + (index * span) / (x.length - 1);
  return (
    <svg
      viewBox={`0 0 ${PLOT.width} ${PLOT.height}`}
      role="img"
      aria-label={label}
      className="block h-auto w-full"
    >
      <Axes x={x} xAt={xAt} ticks={ticks} y={y} unit={unit} format={format} />
      {series.map((line, index) => (
        <g key={line.name} data-series={line.name}>
          <path
            d={line.values
              .map((value, at) => `${at === 0 ? "M" : "L"}${xAt(at)},${y(value)}`)
              .join(" ")}
            fill="none"
            strokeWidth={2}
            strokeLinejoin="round"
            strokeLinecap="round"
            style={{ stroke: seriesColour(index) }}
          />
          {line.values.map((value, at) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: a value is its place on the line
            <g key={at} data-point>
              <title>{`${line.name}, ${x[at]}: ${format(value)}`}</title>
              <circle cx={xAt(at)} cy={y(value)} r={12} fill="transparent" />
              <circle
                cx={xAt(at)}
                cy={y(value)}
                r={4}
                strokeWidth={2}
                style={{ fill: seriesColour(index), stroke: "var(--ds-surface)" }}
              />
            </g>
          ))}
        </g>
      ))}
    </svg>
  );
}
