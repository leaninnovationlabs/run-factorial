/**
 * The settings drawer: where the four shading ramps turn, and how big a node is.
 *
 * Both controls are here for the same reason — neither is a question you ask of the board, they
 * are questions you ask of the CONTROLS. A threshold and a node size are set once, looked at
 * rarely, and would otherwise take permanent space beside a board whose whole argument is that
 * every pixel is a ticket. It opens in the drawer (decision 50).
 *
 * `SettingsPanel` is the contents and holds every rule; `SettingsDrawer` only wraps it, so the part
 * with the behaviour in it renders on the server, where a portal cannot.
 */

import { Fragment, useState } from "react";
import { Button, Drawer, SegmentedControl, TextField } from "../ui";
import { DENSITIES } from "./ModeBar";
import {
  DEFAULT_THRESHOLDS,
  METRIC_SPECS,
  METRICS,
  type Metric,
  type Threshold,
  type Thresholds,
} from "./model";

export interface SettingsProps {
  thresholds: Thresholds;
  onThresholds: (next: Thresholds) => void;
  density: number;
  onDensity: (index: number) => void;
}

export function SettingsPanel({ thresholds, onThresholds, density, onDensity }: SettingsProps) {
  const set = (metric: Metric, edge: keyof Threshold, value: number) =>
    onThresholds({ ...thresholds, [metric]: { ...thresholds[metric], [edge]: value } });

  return (
    <div className="space-y-6">
      <section className="space-y-3">
        <div className="space-y-1">
          <h3 className="text-heading-xs text-default">Metric shading thresholds</h3>
          <p className="text-body-sm text-subtle">
            Each metric shades the board in four bands. Values under <strong>Lower</strong> stay
            neutral. Values at <strong>Upper</strong> or above get the strongest shade. The range
            between them splits at its midpoint. Each metric has its own pair.
          </p>
        </div>

        {/* Three columns, one grid: the two inputs of every metric line up down the page, which
            is the point of putting four ramps in one place — you set them against each other. */}
        <div className="grid grid-cols-[1fr_6rem_6rem] items-center gap-x-3 gap-y-2">
          <span className="text-body-sm font-semibold text-subtle">Metric</span>
          <span className="text-body-sm font-semibold text-subtle">Lower</span>
          <span className="text-body-sm font-semibold text-subtle">Upper</span>

          {METRICS.map((metric) => {
            const spec = METRIC_SPECS[metric];
            const pair = thresholds[metric];
            return (
              <Fragment key={metric}>
                <span className="flex items-baseline gap-2">
                  <span className="text-body font-medium text-default">{spec.label}</span>
                  <span className="text-body-sm text-subtle">{spec.noun}</span>
                </span>
                <NumberInput
                  label={`${spec.label} lower threshold`}
                  value={pair.lower}
                  step={spec.step}
                  onCommit={(value) => set(metric, "lower", value)}
                />
                <NumberInput
                  label={`${spec.label} upper threshold`}
                  value={pair.upper}
                  step={spec.step}
                  onCommit={(value) => set(metric, "upper", value)}
                />
              </Fragment>
            );
          })}
        </div>

        <Button
          appearance="subtle"
          spacing="compact"
          iconBefore="undo"
          onClick={() => onThresholds(DEFAULT_THRESHOLDS)}
        >
          Reset to defaults
        </Button>
      </section>

      <section className="flex flex-wrap items-center justify-between gap-4 border-t border-default pt-4">
        <div className="space-y-1">
          <h3 className="text-heading-xs text-default">Ticket size</h3>
          <p className="text-body-sm text-subtle">
            The size of each ticket's square. Larger is easier to read. Smaller fits more tickets on
            one screen.
          </p>
        </div>
        <SegmentedControl
          label="Ticket size"
          value={String(density)}
          onChange={(value) => onDensity(Number(value))}
          options={DENSITIES.map((size, index) => ({ value: String(index), label: size.label }))}
        />
      </section>
    </div>
  );
}

/**
 * A number field that lets you empty it.
 *
 * Binding `value` straight to a number makes the field un-clearable: delete the `1` of `100` and
 * it snaps back the same keystroke, so `1` can never become `10`. So the draft string is what is
 * displayed while it is being typed, every parse that succeeds commits immediately, and blur
 * drops the draft — which is also what discards a half-typed `-` or an empty field rather than
 * committing a threshold nobody asked for.
 */
function NumberInput({
  label,
  value,
  step,
  onCommit,
}: {
  label: string;
  value: number;
  step: number;
  onCommit: (value: number) => void;
}) {
  const [draft, setDraft] = useState<string | null>(null);

  return (
    <TextField
      type="number"
      min={0}
      step={step}
      aria-label={label}
      spacing="compact"
      className="tabular-nums"
      value={draft ?? String(value)}
      onChange={(event) => {
        const text = event.target.value;
        setDraft(text);
        const parsed = Number(text);
        if (text.trim() !== "" && Number.isFinite(parsed) && parsed >= 0) onCommit(parsed);
      }}
      onBlur={() => setDraft(null)}
    />
  );
}

/** The panel in the app's drawer. Nothing but the wrapper — see the note at the top of the file. */
export function SettingsDrawer({ onClose, ...panel }: SettingsProps & { onClose: () => void }) {
  return (
    <Drawer title="Board settings" subtitle="Shading thresholds and ticket size." onClose={onClose}>
      <SettingsPanel {...panel} />
    </Drawer>
  );
}
