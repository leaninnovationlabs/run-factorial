/**
 * Two rules of "Spend by step" no page test can see. A step keeps its colour whatever the
 * window: the look follows the process config, so a window without the first step must not
 * hand its colour to the next. And the time view draws only time that has passed: an attempt
 * still running has no active seconds, so it draws nothing rather than a 2px sliver, and two
 * attempts that overlap by a tick draw no gap between them.
 */

import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import type { TicketStepCosts } from "../../api/types";
import { dashboard } from "./dashboard.fixture";
import { partsOf, StepCosts } from "./StepCosts";

describe("StepCosts", () => {
  it("keeps a step's colour when the window has no run of the step before it", () => {
    const base = dashboard();
    const windowed = dashboard({
      steps: base.steps.filter((step) => step.step !== "plan"),
      step_costs: base.step_costs.map((ticket) => ({
        ...ticket,
        segments: ticket.segments.filter((segment) => segment.step !== "plan"),
      })),
    });
    const html = renderToString(
      <MemoryRouter>
        <StepCosts data={windowed} stepOrder={["plan", "build"]} />
      </MemoryRouter>,
    );
    // build is the lane's second agent step, so series 2, as it is over all time.
    expect(html).toMatch(/data-segment="build-1"[^>]*--look:var\(--dash-series-2\)/);
    expect(html).not.toContain("--dash-series-1");
  });

  it("draws no segment for an attempt still running, and no gap where attempts overlap", () => {
    const [ticket] = dashboard().step_costs;
    const plan = ticket?.segments[0];
    if (!ticket || !plan) throw new Error("the fixture has no step costs");
    const running: TicketStepCosts = {
      ...ticket,
      segments: [
        plan,
        // Started half a minute before the plan attempt's recorded end, and not ended yet.
        {
          ...plan,
          step: "build",
          started_at: "2026-09-29T09:07:00Z",
          ended_at: null,
          seconds: null,
          parked_seconds: 0,
        },
      ],
    };
    const parts = partsOf(running, ["plan", "build"], "time");
    expect(parts.map((part) => part.key)).toEqual(["plan-1"]);
    for (const part of parts) expect(part.value, part.key).toBeGreaterThan(0);
  });
});
