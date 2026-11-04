/**
 * One measure across steps, tickets or projects: a group of bars per label, one bar per series,
 * from a zero baseline. A bar is at most 24px wide, square at the baseline and rounded at its end,
 * with a 2px gap between neighbours. Each bar names its series, label and value when the pointer
 * rests on it; the card's table holds the same numbers for every other reader.
 */

import { Axes, type ChartSeries, PLOT, seriesColour, valueScale } from "./ChartFrame";

const WIDEST = 24;
const GAP = 2;
const ROUND = 4;

/** A bar from `base` to `end`, rounded at `end` only, whichever side of the baseline it is. */
export function barPath(left: number, width: number, base: number, end: number): string {
  const r = Math.min(ROUND, width / 2, Math.abs(end - base));
  const toward = end < base ? 1 : -1;
  const right = left + width;
  return [
    `M${left},${base}`,
    `V${end + toward * r}`,
    `Q${left},${end} ${left + r},${end}`,
    `H${right - r}`,
    `Q${right},${end} ${right},${end + toward * r}`,
    `V${base}`,
    "Z",
  ].join(" ");
}

export function BarChart({
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
  const band = (PLOT.width - PLOT.left - PLOT.right) / x.length;
  const xAt = (index: number) => PLOT.left + (index + 0.5) * band;
  const width = Math.max(
    1,
    Math.min(WIDEST, (band * 0.8 - GAP * (series.length - 1)) / series.length),
  );
  const group = width * series.length + GAP * (series.length - 1);
  const base = y(0);
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
          {line.values.map((value, at) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: a value is its place in the group
            <g key={at} data-bar>
              <title>{`${line.name}, ${x[at]}: ${format(value)}`}</title>
              <path
                d={barPath(xAt(at) - group / 2 + index * (width + GAP), width, base, y(value))}
                style={{ fill: seriesColour(index) }}
              />
            </g>
          ))}
        </g>
      ))}
    </svg>
  );
}
