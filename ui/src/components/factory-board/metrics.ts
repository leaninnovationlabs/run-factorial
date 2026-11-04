/** The shading metrics: what each measures, where its ramp turns, and how it is written. */

import { type IssueNode, retriesOf } from "./lanes";
import type { Mode } from "./modes";

// --- the shading metrics -------------------------------------------------------------------
//
// Four questions of the same shape: pick a number every ticket has, put it on a four-band ramp,
// and shade the whole board by which band it lands in. Retries came first; cost, runtime and
// tokens are the same machinery pointed at the `step_run` totals.

export type Metric = "retry" | "cost" | "time" | "tokens";

export const METRICS: Metric[] = ["retry", "cost", "time", "tokens"];

/** The ticket's value for a metric. The one place a metric touches the node's shape. */
export function metricValue(node: IssueNode, metric: Metric): number {
  switch (metric) {
    case "retry":
      return retriesOf(node);
    case "cost":
      return node.costUsd;
    case "time":
      return node.minutes;
    default:
      return node.tokens;
  }
}

/**
 * Where a metric's ramp turns. Two numbers, four bands: under `lower` is fine, over `upper` is
 * bad, and the span between them splits at its midpoint so the ramp keeps the four shades the
 * legend and the stylesheet already carry.
 */
export interface Threshold {
  lower: number;
  upper: number;
}

export type Thresholds = Record<Metric, Threshold>;

/**
 * Where the ramps sit until an operator moves them.
 *
 * Retry's pair is not a guess: 1 and 3 reproduce exactly the fixed ramp this replaced — 0 clean,
 * 1, 2, then 3-and-up — so turning retries into a configurable metric changed no pixel until
 * somebody changed a number. The other three are opening positions for a prototype, and are the
 * reason the settings drawer exists at all.
 */
export const DEFAULT_THRESHOLDS: Thresholds = {
  retry: { lower: 1, upper: 3 },
  cost: { lower: 1, upper: 5 },
  time: { lower: 60, upper: 480 },
  tokens: { lower: 250_000, upper: 1_000_000 },
};

/**
 * The band a value falls in.
 *
 * Open-ended at both ends on purpose: everything under `lower` is equally fine and everything
 * over `upper` is equally bad news, because a ramp that kept resolving past the point of concern
 * would spend its contrast where nobody is looking.
 *
 * `upper <= lower` is not rejected — it collapses to two bands, which is a coherent thing for an
 * operator to ask for and not worth a validation error.
 */
export function heatOf(value: number, threshold: Threshold): 0 | 1 | 2 | 3 {
  if (value < threshold.lower) return 0;
  if (value >= threshold.upper) return 3;
  return value < (threshold.lower + threshold.upper) / 2 ? 1 : 2;
}

export function bandOf(node: IssueNode, metric: Metric, thresholds: Thresholds): 0 | 1 | 2 | 3 {
  return heatOf(metricValue(node, metric), thresholds[metric]);
}

export interface MetricSpec {
  id: Metric;
  /** The mode that shades by this metric. */
  mode: Mode;
  /** The button. Upper-cased because the four read as one family of the same control. */
  label: string;
  /** What the numbers are, for the legend caption and the settings rows. */
  noun: string;
  hint: string;
  /** Increment for the settings inputs — one that moves the ramp visibly in one press. */
  step: number;
  /** Compact, for the legend, where the caption already says what the numbers are. */
  format(value: number): string;
  /** A figure that has to stand alone — the project gutter, a row's spend, either line of a
   *  metric button — so it carries its own unit rather than borrowing a caption's. */
  formatSum(value: number): string;
}

/* Always to the cent, however large. The figure is read stacked against a second one — a total
   over the worst single ticket — and a `$120` that dropped its decimals while the line under it
   kept them made the two different lengths for no reason a reader could see. Cents are also the
   unit a bill is reasoned about in, which is why `format.ts` prints them for a run. */
function formatCost(value: number): string {
  return `$${Math.max(0, value).toFixed(2)}`;
}

function formatMinutes(value: number): string {
  const minutes = Math.max(0, value);
  if (minutes < 60) return `${Math.round(minutes)}m`;
  const hours = Math.floor(minutes / 60);
  const rest = Math.round(minutes % 60);
  return rest === 0 ? `${hours}h` : `${hours}h ${rest}m`;
}

function formatTokens(value: number): string {
  const count = Math.max(0, value);
  if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(1)}M`;
  if (count >= 1_000) return `${Math.round(count / 1_000)}k`;
  return String(Math.round(count));
}

export const METRIC_SPECS: Record<Metric, MetricSpec> = {
  retry: {
    id: "retry",
    mode: "shade_retry",
    label: "Retries",
    noun: "retries",
    hint: "Attempts beyond the first at the current step",
    step: 1,
    format: (value) => String(Math.max(0, Math.round(value))),
    formatSum: (value) => {
      const count = Math.max(0, Math.round(value));
      return `${count} ${count === 1 ? "retry" : "retries"}`;
    },
  },
  cost: {
    id: "cost",
    mode: "shade_cost",
    label: "Cost",
    noun: "cost",
    hint: "Dollars spent across every run of this ticket",
    step: 0.5,
    format: formatCost,
    formatSum: formatCost,
  },
  time: {
    id: "time",
    mode: "shade_time",
    label: "Time",
    noun: "duration",
    hint: "Agent minutes across every run of this ticket",
    step: 15,
    format: formatMinutes,
    formatSum: formatMinutes,
  },
  tokens: {
    id: "tokens",
    mode: "shade_tokens",
    label: "Tokens",
    noun: "tokens",
    hint: "Tokens used across every run of this ticket",
    step: 50_000,
    format: formatTokens,
    formatSum: (value) => `${formatTokens(value)} tok`,
  },
};

/**
 * The legend, derived from the ramp rather than written beside it.
 *
 * Every entry carries a `probe` — a value that genuinely lands in the band it is labelled with —
 * so a test can hold the key and `heatOf` together and neither can drift.
 */
export function rampBands(
  metric: Metric,
  thresholds: Thresholds,
): { band: 0 | 1 | 2 | 3; label: string; probe: number }[] {
  const { lower, upper } = thresholds[metric];
  const mid = (lower + upper) / 2;
  const show = METRIC_SPECS[metric].format;
  return [
    { band: 0, label: `Under ${show(lower)}`, probe: lower - 1 },
    { band: 1, label: `${show(lower)} to ${show(mid)}`, probe: lower },
    { band: 2, label: `${show(mid)} to ${show(upper)}`, probe: mid },
    { band: 3, label: `${show(upper)}+`, probe: upper },
  ];
}

/** The figure a row or a square states for a metric. Cost wears a tilde when any part of the
 *  ticket's sum is a list-price estimate; the other metrics are measured. */
export function formatMetric(node: IssueNode, metric: Metric): string {
  const figure = METRIC_SPECS[metric].formatSum(metricValue(node, metric));
  return metric === "cost" && node.costEstimated ? `~${figure}` : figure;
}

/**
 * What one metric adds up to across a set of tickets.
 *
 * `max` is the single worst ticket, and it is the number a total cannot give — a band of four
 * hundred cheap tickets and a band with one runaway can sum the same, and only one of them is a
 * problem you can go and fix.
 */
export interface MetricSummary {
  /** Tickets summarised. 0 for a focus on an empty node group. */
  nodes: number;
  total: number;
  max: number;
}

export function summariseMetric(nodes: IssueNode[], metric: Metric): MetricSummary {
  let total = 0;
  let max = 0;
  for (const node of nodes) {
    const value = metricValue(node, metric);
    total += value;
    if (value > max) max = value;
  }
  return { nodes: nodes.length, total, max };
}
