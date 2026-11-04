import type { ReactNode } from "react";
import type { Span } from "../../api/types";
import { cx, Icon, VisuallyHidden } from "../ui";

/** Which way is good: up for more done, down for a cost or a wait, and neither for spend, where
 *  more can simply mean more work. */
export type Better = "up" | "down" | null;

const WEEKS: Record<Exclude<Span, "all">, string> = { "4w": "4 weeks", "12w": "12 weeks" };

const TONES = {
  good: "text-success",
  bad: "text-danger",
  neutral: "text-subtle",
  muted: "text-subtlest",
} as const;

/**
 * A card's figure against the period of the same length before this one. `previous` is
 * `undefined` when there is no such period, and `null`, like `current`, when the figure has no
 * value on that side, such as a median with nothing done: then there is nothing to compare.
 */
export function ChangeLine({
  span,
  current,
  previous,
  format,
  round = (magnitude) => magnitude,
  better,
}: {
  span: Span;
  current: number | null;
  previous: number | null | undefined;
  format: (magnitude: number) => string;
  /** Rounds a figure exactly as the card's headline does, so the change is the difference of the
   *  two figures as printed. */
  round?: (magnitude: number) => number;
  better: Better;
}) {
  if (span === "all") return null;
  if (previous === undefined) return <Line tone="muted">Nothing to compare with yet</Line>;
  if (current === null || previous === null) return null;
  const prior = `the previous ${WEEKS[span]}`;
  // Equal as the card prints them, so figures that read alike say no change, not "▲ $0.01".
  if (format(current) === format(previous)) return <Line tone="neutral">Same as {prior}</Line>;
  let delta = round(current) - round(previous);
  // Figures that print apart but round to the same value, such as 0 and under half a cent.
  if (format(Math.abs(delta)) === format(0)) delta = current - previous;
  const text = format(Math.abs(delta));
  const up = delta > 0;
  const tone = better === null ? "neutral" : (better === "up") === up ? "good" : "bad";
  return (
    <Line tone={tone}>
      <Icon glyph={up ? "arrow-up" : "arrow-down"} size="small" className="inline align-baseline" />
      <VisuallyHidden>{up ? "Up" : "Down"}</VisuallyHidden> {text} compared with {prior}
    </Line>
  );
}

function Line({ tone, children }: { tone: keyof typeof TONES; children: ReactNode }) {
  return (
    <p data-change={tone} className={cx("text-body-sm", TONES[tone])}>
      {children}
    </p>
  );
}
