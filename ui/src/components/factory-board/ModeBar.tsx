/**
 * The board's control strip: the metric buttons, the ramp key that explains them, the row that
 * says what is applied and clears it, the count panel, and the React binding for the hover-intent
 * controller that guards the buttons.
 *
 * Four buttons are mounted, one per metric. A shade mode is not a filter and has no column of its
 * own: it recolours every node by what that ticket has spent, which is a question no arrangement
 * of the lanes can answer.
 *
 * The timing rules live in `modePreview.ts`, framework-free, because this project's test
 * environment is `node` and there is no DOM to render a hook into. All this file does is turn
 * pointer and keyboard events into that controller's vocabulary, and read its snapshot back
 * through `useSyncExternalStore`.
 */

import { useEffect, useMemo, useSyncExternalStore } from "react";
import { figureSizers } from "../../format";
// Aliased: `Metric` in this file is the model's "which metric is being shaded", not the figure.
import { Button, Icon, Metric as MetricFigure, Pressable, Tag, TagGroup, Tooltip } from "../ui";
import { describeFocus, type Focus } from "./focus";
import type { LaneSort } from "./layout";
import {
  DEFAULT_THRESHOLDS,
  LANE_LABELS,
  METRIC_SPECS,
  type Metric,
  type Mode,
  type ModeButton,
  type ProjectBand,
  rampBands,
  type Thresholds,
} from "./model";
import { createModePreview, type ModePreview, type ModePreviewState } from "./modePreview";

export interface BoardMode extends ModePreviewState {
  controller: ModePreview;
}

/** `initial` is read once, at mount: after that the controller is the one that knows. */
export function useBoardMode(initial: Mode = "idle"): BoardMode {
  // biome-ignore lint/correctness/useExhaustiveDependencies: the starting mode, read once
  const controller = useMemo(() => createModePreview({ locked: initial }), []);
  useEffect(() => () => controller.dispose(), [controller]);
  const state = useSyncExternalStore(
    controller.subscribe,
    controller.getState,
    controller.getState,
  );
  return { ...state, controller };
}

/**
 * `buttons` is built by `modeButtons` from the FOCUSED set of tickets, so every figure here
 * narrows to whatever is bracketed on the board. The bar itself does no arithmetic — it is the
 * one place where a hover both previews a view and changes the numbers explaining it, and doing
 * the sums here would put that coupling somewhere untestable.
 */
export function ModeBar({ buttons, mode }: { buttons: ModeButton[]; mode: BoardMode }) {
  const { controller, locked } = mode;
  return (
    // Enter and leave are bound to the bar, not to the buttons: the exit grace has to span the
    // gaps between them, or crossing from one button to the next would flicker back to idle.
    // A group of toggles rather than a toolbar: it takes Tab between its buttons, not arrows.
    <fieldset
      className="factory-board__bar"
      aria-label="Shade the board by"
      onMouseEnter={() => controller.enterBar()}
      onMouseLeave={() => controller.leaveBar()}
    >
      {buttons.map((button) => (
        // What the mode means is on the tooltip, the face having been given over to what the
        // selection actually holds.
        <Tooltip key={button.id} content={button.hint} describes>
          <Pressable
            className="factory-board__mode"
            data-tone={button.id}
            isSelected={locked === button.id}
            onMouseEnter={() => controller.hover(button.id)}
            onMouseLeave={() => controller.unhover()}
            onFocus={() => controller.focus(button.id)}
            onBlur={() => controller.blur()}
            onClick={() => controller.select(button.id)}
          >
            <span className="factory-board__mode-label">{button.label}</span>
            {/* The live figures, against the right edge so the four buttons make one column a
                reader can run down. Two grid columns, so the labels line up with each other and
                the figures line up with each other — a per-row wrapper can only align a row
                against itself. */}
            <span className="factory-board__mode-figures">
              {button.figures === null ? (
                <span className="factory-board__mode-empty">Nothing selected</span>
              ) : (
                <>
                  <span className="factory-board__mode-key">Total</span>
                  <span className="factory-board__mode-total">
                    <MetricFigure figureSizers={sizersFor(button)}>
                      {button.figures.total}
                    </MetricFigure>
                  </span>
                  <span className="factory-board__mode-key">Max</span>
                  <span className="factory-board__mode-max">
                    <MetricFigure figureSizers={sizersFor(button)}>
                      {button.figures.max}
                    </MetricFigure>
                  </span>
                </>
              )}
            </span>
          </Pressable>
        </Tooltip>
      ))}
    </fieldset>
  );
}

/**
 * Every figure a button is about to draw, gathered per unit from both of its lines.
 *
 * Both lines, gathered together and each given the whole set, so a unit's box is one size on
 * whichever line it appears — see `format.ts`'s `figureSizers`. TIME is the case that needs it:
 * `1h 30m` over `2h` has two boxes on one line and one on the other.
 */
function sizersFor(button: ModeButton): Record<string, string[]> | undefined {
  return button.figures === null
    ? undefined
    : figureSizers([button.figures.total, button.figures.max]);
}

/**
 * What is applied to the board, and the ways back out: one removable tag per thing applied, and
 * "Reset view" for all of them at once. Reset earns its place because the three are independent —
 * you can arrive at "cost shading on a focused project, re-ranked by Error" one click at a time
 * and want out of all of it without deciding which to undo first.
 *
 * The row reserves its height and Reset stays mounted, so nothing below it moves when a tag comes
 * or goes. Reset is `aria-disabled` rather than `disabled` while there is nothing to reset: it
 * stays focusable, ignores the press, and points at the row's own words for why (`id`).
 *
 * A shade mode counts as applied only when it is PINNED. A hover preview evaporates the moment the
 * cursor leaves, so there is nothing to clear, and a tag that came and went as the cursor crossed
 * the metric buttons would strobe.
 */
export function AppliedRow({
  id,
  metric,
  focus,
  laneSort,
  projects,
  onClearMetric,
  onClearFocus,
  onClearSort,
  onReset,
}: {
  /** The id of the words saying what is applied, which Reset points at. */
  id: string;
  /** The pinned metric, or null. */
  metric: Metric | null;
  focus: Focus | null;
  laneSort: LaneSort | null;
  projects: ProjectBand[];
  onClearMetric: () => void;
  onClearFocus: () => void;
  onClearSort: () => void;
  onReset: () => void;
}) {
  const applied = metric !== null || focus !== null || laneSort !== null;
  return (
    <div className="flex min-h-8 flex-wrap items-center gap-2">
      <div id={id} className="flex flex-wrap items-center gap-1">
        {applied ? (
          <TagGroup label="Applied to the board">
            {metric !== null && (
              <Tag onRemove={onClearMetric} removeLabel="Clear metric">
                Metric: {METRIC_SPECS[metric].label}
              </Tag>
            )}
            {focus !== null && (
              <Tag onRemove={onClearFocus} removeLabel="Clear focus">
                Focus: {describeFocus(focus, projects)}
              </Tag>
            )}
            {laneSort !== null && (
              <Tag
                onRemove={onClearSort}
                removeLabel="Clear sort"
                elemBefore={
                  <Icon
                    glyph={laneSort.dir === "asc" ? "arrow-up" : "arrow-down"}
                    size="small"
                    label={laneSort.dir === "asc" ? "fewest first" : "most first"}
                  />
                }
              >
                Sort: {LANE_LABELS[laneSort.lane]}
              </Tag>
            )}
          </TagGroup>
        ) : (
          <span className="text-body-sm text-subtle">Nothing is applied to the board.</span>
        )}
      </div>
      <Button
        spacing="compact"
        iconBefore="undo"
        aria-disabled={applied ? undefined : true}
        aria-describedby={id}
        onClick={onReset}
      >
        Reset view
      </Button>
    </div>
  );
}

/**
 * The active ramp, spelled out.
 *
 * Four shades mean nothing without a key, and the key has to name the actual numbers now that an
 * operator sets them: a band reading "1 – 2" after the retry thresholds move is the only way to
 * see what the board is telling you. Built from `rampBands`, which derives the breaks from the
 * same two numbers `heatOf` splits on, so the key cannot claim a boundary the board does not use.
 *
 * Always mounted, always visible, and always the SAME SIZE. The ramp is rendered whatever the
 * mode: with no metric on, its bands are merely made invisible and the placeholder is laid over
 * them. That is why the box cannot change height when a metric is picked — the thing setting its
 * height is the same four bands and caption in both states, so there is no arithmetic to keep in
 * step and nothing below it shifts. Hiding by `visibility` rather than by not rendering is the
 * whole trick: a hidden element still occupies its space.
 *
 * With no metric the labels are formatted from `retry`, which is arbitrary and invisible — the
 * width is fixed by the stylesheet, so the numbers behind the placeholder set nothing.
 */
export function HeatLegend({
  metric,
  thresholds = DEFAULT_THRESHOLDS,
}: {
  /** The metric being shaded, or null when no shade mode is on. */
  metric: Metric | null;
  thresholds?: Thresholds;
}) {
  const shown = metric ?? "retry";

  return (
    <div className="factory-board__legend" data-on={metric !== null}>
      {rampBands(shown, thresholds).map((entry) => (
        <span key={entry.band} className="factory-board__legend-band">
          <span className="factory-board__legend-swatch" data-look={`heat-${entry.band}`} />
          {/* Its own element so it can ellipsise: the key's box is fixed, and a threshold an
              operator set wide enough must shorten rather than push the buttons about. */}
          <span className="factory-board__legend-label">{entry.label}</span>
        </span>
      ))}
      <span className="factory-board__legend-caption">{METRIC_SPECS[shown].label}</span>
      {metric === null && <span className="factory-board__legend-idle">Select a metric</span>}
    </div>
  );
}

/**
 * Node edge in px, with the matching gap. Ascending, so the switch reads as a scale.
 *
 * The gap grows with the node rather than staying fixed: at 5px a 2px gap is already a fifth of
 * the pitch, and holding it there while the squares tripled would close the lattice into a solid
 * block. What the board draws is a grid of separate tickets, and the gaps are what say so.
 */
export const DENSITIES = [
  { node: 5, gap: 2, label: "Small" },
  { node: 6, gap: 2, label: "Medium" },
  { node: 7, gap: 3, label: "Large" },
] as const;

/**
 * Index into `DENSITIES` the board opens at, until an operator sets one — see `prefs.ts`.
 *
 * Named rather than left as an index, because the default is a judgement about legibility and not
 * a consequence of the array's order — Medium still reads as a square rather than as a dot, and it
 * is the size at which a tall factory fits the frame without the board having to be scrolled to
 * be taken in, which is the whole argument for drawing it. Large is there for a reader who wants a
 * bigger target; Small for a screen that has to hold more. `factory-board.css` holds the matching
 * `--fb-node` / `--fb-gap`.
 *
 * The labels are sizes rather than pixel counts because the number is not the choice
 * being made: a reader is trading how much board fits against how well a square reads, and
 * "5px" states the mechanism instead of the trade.
 */
export const DEFAULT_DENSITY = 1;

/**
 * How many issues the board is drawing, and across how many projects.
 *
 * Not interactive, and deliberately beside the metric buttons rather than under the board: it is
 * the denominator every figure on those buttons is a part of, and a count that has to be scrolled
 * to is a count nobody reads.
 */
export function StatsPanel({ issues, projects }: { issues: number; projects: number }) {
  // Projects above issues: it is the coarser of the two, and the pair reads as a hierarchy —
  // this many projects, holding this many issues — rather than as two unrelated tallies.
  return (
    <div className="factory-board__stats">
      <span className="factory-board__stat">
        <span className="factory-board__stat-value">{projects}</span>
        <span className="factory-board__stat-label">{projects === 1 ? "project" : "projects"}</span>
      </span>
      <span className="factory-board__stat">
        <span className="factory-board__stat-value">{issues}</span>
        <span className="factory-board__stat-label">{issues === 1 ? "ticket" : "tickets"}</span>
      </span>
    </div>
  );
}
