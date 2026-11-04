/**
 * A formatted metric — `$0.94`, `57m 51s`, `41.9K in / 9.2K out` — with each figure boxed with the
 * unit that measures it, and a caption that says what is being measured.
 *
 * The figure is what a reader compares between rows; the unit only says how to read it. Setting
 * the units apart lets the eye run down a column of figures instead of re-reading the same letters
 * on every line. The caption ("Tokens", "Cost (est.)") used to live only in a native tooltip, which
 * a keyboard or a touch screen never shows: it is text now, drawn or read out.
 */

import { metricPieces, sizerUnit } from "../../format";
import { cx } from "./cx";

/**
 * One figure and its unit, boxed together: the unit half filled, the figure half clear, both
 * inside one outline. The unit box is at least as wide as it is tall, so a single mark (`$`) sits
 * in a square and a word (`out`) grows the box into a rectangle rather than being squeezed.
 *
 * `sizers` is what lets two of these boxes stack. It is every figure the set writes for this unit
 * — from `format.ts`'s `figureSizers` — and all of them are laid into the figure's one cell, the
 * real one over the rest, so the browser sizes the cell to the widest ACTUAL rendering. Given the
 * same set, a `$8.00` and a `$10.00` come out the same size and their filled halves land on the
 * same pixel. Measuring rather than counting characters is the whole point: `1.0M` and `900k` are
 * both four characters and the first is visibly wider.
 *
 * The figure is right-aligned in that room, so a column of stacked boxes lines up on the digits a
 * reader is comparing. Without `sizers` the box hugs its figure, which is right everywhere else.
 *
 * The `data-metric-*` attributes are the only stable handle on the parts, for a layout — or a test
 * — that needs to reach one; `data-metric-value` is the figure a reader actually sees, as against
 * the sizers sharing its cell. The unit is filled with `--ds-background-neutral-pressed`, a grey
 * quiet enough that a row of metrics does not out-shout the status beside it, with default ink
 * because subtle ink falls short of 4.5:1 on it. The outline is drawn in the fill so the two
 * halves read as one.
 */
function MetricBox({
  value,
  unit,
  unitFirst,
  sizers,
}: {
  value: string;
  unit: string;
  unitFirst: boolean;
  sizers?: string[];
}) {
  const figure = (
    <span data-metric-figure className="grid items-center justify-items-end px-1">
      {sizers?.map((sizer) => (
        <span key={sizer} aria-hidden="true" className="invisible col-start-1 row-start-1">
          {sizer}
        </span>
      ))}
      <span data-metric-value className="col-start-1 row-start-1">
        {value}
      </span>
    </span>
  );
  const measure = (
    <span
      data-metric-unit
      className="flex min-w-[1em] items-center justify-center bg-neutral-pressed px-px font-bold text-default"
    >
      {UNIT_LABELS[unit] ?? unit}
    </span>
  );
  return (
    <span
      data-metric-box
      className="inline-flex items-stretch overflow-hidden rounded-xs border border-[var(--ds-background-neutral-pressed)] leading-none"
    >
      {unitFirst ? measure : figure}
      {unitFirst ? figure : measure}
    </span>
  );
}

/**
 * Units written shorter in the box than in the string they came from. `retries` is the only one,
 * and for width alone: the word is four times the figure it follows, in a box the size of a `$`.
 * "×" reads as "times" after a count, which is what a retry count is.
 */
const UNIT_LABELS: Record<string, string> = { retries: "x", retry: "x" };

export function Metric({
  children,
  label,
  labelHidden = false,
  figureSizers,
  size = "small",
}: {
  children: string;
  /** What is measured: drawn before the figures, or read out when `labelHidden`. */
  label?: string;
  labelHidden?: boolean;
  /** Every figure written for each unit, from `format.ts`'s `figureSizers`, when this metric is
   *  one of a set that has to stack. Units it does not name hug their own figure as usual. */
  figureSizers?: Record<string, string[]>;
  size?: "small" | "medium";
}) {
  return (
    <span
      data-metric
      className={cx(
        "inline-flex items-center gap-x-1 tabular-nums",
        size === "medium" ? "text-body" : "text-body-sm",
      )}
    >
      {label && (
        <span className={labelHidden ? "sr-only" : "text-subtlest"}>
          {label}
          {labelHidden ? " " : null}
        </span>
      )}
      {metricPieces(children).map((piece, index) =>
        piece.kind === "text" ? (
          // biome-ignore lint/suspicious/noArrayIndexKey: the split is a pure function of the string, so a piece IS its position
          <span key={index}>{piece.text}</span>
        ) : (
          <MetricBox
            // biome-ignore lint/suspicious/noArrayIndexKey: the split is a pure function of the string, so a piece IS its position
            key={index}
            value={piece.value}
            unit={piece.unit}
            unitFirst={piece.unitFirst}
            sizers={figureSizers?.[sizerUnit(piece.unit)]}
          />
        ),
      )}
    </span>
  );
}
