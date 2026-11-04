/**
 * A chart the assistant drew (`factorial_chart`): `line` over time, `bar` or `grouped_bar` across
 * steps, tickets or projects. The title, the unit and the source come from the card; View data
 * shows the same numbers, in the same words, as a table.
 */

import { useState } from "react";
import type { ChartPayload } from "../../../api/types";
import { BarChart } from "../../charts/BarChart";
import { ChartFrame } from "../../charts/ChartFrame";
import { LineChart } from "../../charts/LineChart";
import { Button, type Column, DataTable, VisuallyHidden } from "../../ui";

const KINDS = new Set(["line", "bar", "grouped_bar"]);
const UNITS = new Set(["USD", "minutes", "hours", "tickets", "tokens"]);

const USD = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const WHOLE = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const TENTHS = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

function formatValue(value: number, unit: ChartPayload["unit"]): string {
  switch (unit) {
    case "USD":
      return USD.format(value);
    case "minutes":
      return `${TENTHS.format(value)} min`;
    case "hours":
      return `${TENTHS.format(value)} h`;
    default:
      return WHOLE.format(value);
  }
}

/** The card as the tool wrote it, or null for a payload that is not one: a chart draws from
 *  these numbers alone, so a series of the wrong length is not drawn at all. */
function asChart(payload: object): ChartPayload | null {
  const { kind, title, unit, x, series, source } = payload as Record<string, unknown>;
  if (typeof kind !== "string" || !KINDS.has(kind)) return null;
  if (typeof unit !== "string" || !UNITS.has(unit)) return null;
  if (typeof title !== "string" || typeof source !== "string") return null;
  if (!Array.isArray(x) || x.length === 0 || !x.every((label) => typeof label === "string")) {
    return null;
  }
  if (!Array.isArray(series) || series.length === 0) return null;
  const valid = series.every(
    (line) =>
      typeof line?.name === "string" &&
      Array.isArray(line.values) &&
      line.values.length === x.length &&
      line.values.every((value: unknown) => typeof value === "number" && Number.isFinite(value)),
  );
  return valid ? (payload as unknown as ChartPayload) : null;
}

export function ChartCard({ payload }: { payload: object }) {
  const [showData, setShowData] = useState(false);
  const chart = asChart(payload);
  if (!chart) return null;
  const format = (value: number) => formatValue(value, chart.unit);
  const label = `${chart.title}, ${chart.series.map((line) => line.name).join(", ")}, in ${chart.unit}`;
  const Chart = chart.kind === "line" ? LineChart : BarChart;
  const head: Column[] = [
    { key: "x", content: <VisuallyHidden>Label</VisuallyHidden> },
    ...chart.series.map((line, index) => ({
      key: `s${index}`,
      content: line.name,
      align: "end" as const,
    })),
  ];
  return (
    <ChartFrame
      title={chart.title}
      series={chart.series}
      mark={chart.kind === "line" ? "line" : "bar"}
      actions={
        <Button
          appearance="subtle"
          spacing="compact"
          aria-expanded={showData}
          onClick={() => setShowData((shown) => !shown)}
        >
          {showData ? "Hide data" : "View data"}
        </Button>
      }
      footer={`From ${chart.source}`}
    >
      <Chart label={label} x={chart.x} series={chart.series} unit={chart.unit} format={format} />
      {showData && (
        <DataTable
          label={`${chart.title}, as a table`}
          head={head}
          rows={chart.x.map((name, index) => ({ name, index }))}
          rowKey={(row) => row.index}
          cells={(row) =>
            Object.fromEntries([
              ["x", row.name],
              ...chart.series.map((line, index) => [
                `s${index}`,
                format(line.values[row.index] ?? 0),
              ]),
            ])
          }
          emptyView={null}
        />
      )}
    </ChartFrame>
  );
}
