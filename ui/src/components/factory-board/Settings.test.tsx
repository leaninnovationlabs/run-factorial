/**
 * The settings panel's markup, and the rule that makes it worth having: every metric carries its
 * OWN pair of thresholds, and editing one must not disturb another.
 *
 * `SettingsPanel` rather than `SettingsDrawer`: the drawer renders through a portal, which the
 * server renderer refuses.
 */

import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { DEFAULT_THRESHOLDS, METRIC_SPECS, METRICS, type Thresholds } from "./model";
import { SettingsPanel } from "./Settings";

function panel(thresholds: Thresholds = DEFAULT_THRESHOLDS, onThresholds = () => {}) {
  return renderToString(
    <SettingsPanel
      thresholds={thresholds}
      onThresholds={onThresholds}
      density={0}
      onDensity={() => {}}
    />,
  );
}

const count = (html: string, needle: string) => html.split(needle).length - 1;

describe("SettingsPanel", () => {
  it("gives every metric its own lower and upper field", () => {
    const html = panel();
    expect(count(html, 'type="number"')).toBe(METRICS.length * 2);
    for (const metric of METRICS) {
      expect(html).toContain(`${METRIC_SPECS[metric].label} lower threshold`);
      expect(html).toContain(`${METRIC_SPECS[metric].label} upper threshold`);
    }
  });

  it("shows each ramp where it actually sits, not a shared default", () => {
    const html = panel({ ...DEFAULT_THRESHOLDS, cost: { lower: 7, upper: 42 } });
    expect(html).toContain('value="7"');
    expect(html).toContain('value="42"');
    // Retry is untouched and still reads its own pair.
    expect(html).toContain('value="3"');
  });

  it("holds the node size, so the board keeps no control that is set once and read never", () => {
    const html = panel();
    expect(html).toContain('role="group" aria-label="Ticket size"');
    // Named for the trade being made, not for the mechanism: a reader is choosing how much board
    // fits against how well a square reads, and "5px" states the number instead of the choice.
    for (const label of ["Small", "Medium", "Large"]) expect(html).toContain(`>${label}<`);
    expect(html).not.toContain("5px");
    // The one at `density={0}` is the pressed one.
    expect(html).toMatch(/aria-pressed="true"[^>]*>(?:<[^>]+>)*Small</);
  });

  it("steps each field by an increment that moves its own ramp, not by one", () => {
    // A dollar and a token do not share a sensible increment: 50,000 tokens is a nudge and one
    // token is not, while a $50,000 nudge would be absurd.
    const html = panel();
    expect(html).toContain('step="50000"');
    expect(html).toContain('step="0.5"');
  });
});
