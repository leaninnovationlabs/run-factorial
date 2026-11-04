/** The board's view modes, what a slot looks like in each, and the buttons on the rail. */

import { type IssueNode, laneOf } from "./lanes";
import {
  bandOf,
  DEFAULT_THRESHOLDS,
  METRIC_SPECS,
  METRICS,
  type Metric,
  summariseMetric,
  type Thresholds,
} from "./metrics";

// --- view modes ---------------------------------------------------------------------------

export type Mode = "idle" | "shade_retry" | "shade_cost" | "shade_time" | "shade_tokens";

export interface ModeSpec {
  id: Exclude<Mode, "idle">;
  label: string;
  hint: string;
}

/** The mode that shades by each metric, and nothing else. */
const METRIC_OF: Partial<Record<Mode, Metric>> = {
  shade_retry: "retry",
  shade_cost: "cost",
  shade_time: "time",
  shade_tokens: "tokens",
};

export function metricOf(mode: Mode): Metric | null {
  return METRIC_OF[mode] ?? null;
}

export function modeOf(metric: Metric | null): Mode {
  return metric === null ? "idle" : METRIC_SPECS[metric].mode;
}

/** The four shading specs, in metric order — the buttons on the rail. */
export const SHADE_MODES: ModeSpec[] = METRICS.map((metric) => {
  const spec = METRIC_SPECS[metric];
  return { id: spec.mode as Exclude<Mode, "idle">, label: spec.label, hint: spec.hint };
});

// --- appearance ----------------------------------------------------------------------------

/**
 * What one slot on the board LOOKS like: the single string the stylesheet keys off, and the
 * value the crossfade diffs on.
 *
 * Collapsing appearance to one token is what lets the transition be surgical. Two nodes in the
 * same lane swapping places produce the same token in both positions, so nothing is faded — only
 * positions that genuinely change colour do.
 */
export function appearanceOf(
  node: IssueNode,
  mode: Mode,
  thresholds: Thresholds = DEFAULT_THRESHOLDS,
): string {
  const metric = metricOf(mode);
  if (metric !== null) return `heat-${bandOf(node, metric, thresholds)}`;
  return laneOf(node);
}

/** A position holding no ticket. Still drawn — an empty slot is information. */
export const EMPTY = "empty";

/** Everything one button on the rail draws. */
export interface ModeButton {
  id: Exclude<Mode, "idle">;
  label: string;
  /** What the mode means. On the tooltip — the face of the button carries live numbers. */
  hint: string;
  /**
   * The two figures stacked at the button's right edge: what the selection adds up to, over the
   * single worst ticket in it. Null when there is no sum to report — an empty selection.
   *
   * A pair rather than one line: totals rank the projects, and the maximum is what says whether
   * a total is one runaway or four hundred ordinary tickets (see `MetricSummary`).
   *
   * Neither string says WHICH it is — the button draws that as a label beside it, so the two
   * figures start at the same place and can be read down a column.
   */
  figures: { total: string; max: string } | null;
}

/**
 * The buttons, for a given set of tickets.
 *
 * Given the FOCUSED set rather than the whole board, every figure on the rail narrows to what
 * the reader has selected — so the rail stops being a static key and becomes the readout for
 * whatever is bracketed on the board. Pass every node when nothing is focused and it reads as
 * the whole factory, which is the same thing said about a wider selection.
 */
export function modeButtons(specs: ModeSpec[], nodes: IssueNode[]): ModeButton[] {
  return specs.map((spec) => {
    const metric = metricOf(spec.id);
    if (metric === null) return { id: spec.id, label: spec.label, hint: spec.hint, figures: null };
    const summary = summariseMetric(nodes, metric);
    const { formatSum } = METRIC_SPECS[metric];
    return {
      id: spec.id,
      label: spec.label,
      hint: spec.hint,
      figures:
        summary.nodes === 0
          ? null
          : { total: formatSum(summary.total), max: formatSum(summary.max) },
    };
  });
}
