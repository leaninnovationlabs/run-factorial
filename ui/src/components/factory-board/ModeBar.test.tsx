/**
 * The bar's markup, and the contract that now matters most about it: what a button says is a
 * function of the ticket set it was given, so handing it the focused set is what makes the rail
 * a readout rather than a static key.
 *
 * The controller's timing is `modePreview.test.ts`'s job. This file checks that the bar draws
 * what `modeButtons` computed, and that the legend is mounted before the mode it explains is.
 */

import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { AppliedRow, DEFAULT_DENSITY, DENSITIES, HeatLegend, ModeBar } from "./ModeBar";
import { DEFAULT_THRESHOLDS, type IssueNode, modeButtons, SHADE_MODES } from "./model";
import { createModePreview } from "./modePreview";

/** Every metric on the page, as the two halves of its box in the order they are written. */
function metrics(html: string): string[][] {
  return html
    .split("<span data-metric-box")
    .slice(1)
    .map((chunk) => {
      const unit = chunk.match(/data-metric-unit[^>]*>([^<]*)</)?.[1] ?? "";
      const value = chunk.match(/data-metric-value[^>]*>([^<]*)</)?.[1] ?? "";
      return chunk.indexOf("data-metric-unit") < chunk.indexOf("data-metric-value")
        ? [unit, value]
        : [value, unit];
    });
}

function node(over: Partial<IssueNode> = {}): IssueNode {
  return {
    id: 1,
    key: "acme/repo#1",
    title: null,
    projectId: 1,
    step: "build",
    latestRunStatus: null,
    status: "running",
    runs: 1,
    attempt: 1,
    costUsd: 0,
    minutes: 0,
    tokens: 0,
    ...over,
  };
}

function bar(modes = SHADE_MODES, nodes: IssueNode[] = [node()]) {
  const controller = createModePreview();
  const mode = { ...controller.getState(), controller };
  return renderToString(<ModeBar buttons={modeButtons(modes, nodes)} mode={mode} />);
}

const count = (html: string, needle: string) => html.split(needle).length - 1;

describe("ModeBar", () => {
  it("draws one button per spec it is given", () => {
    expect(count(bar(), "data-tone=")).toBe(SHADE_MODES.length);
    expect(count(bar(SHADE_MODES.slice(0, 2)), "data-tone=")).toBe(2);
  });

  it("names each button for its metric alone, in sentence case", () => {
    const html = bar(SHADE_MODES);
    for (const label of ["Retries", "Cost", "Time", "Tokens"]) {
      expect(html).toContain(`factory-board__mode-label">${label}<`);
    }
  });

  it("is a group of toggles, pinning nothing until something is clicked", () => {
    const html = bar(SHADE_MODES);
    expect(html).toMatch(/<fieldset[^>]*aria-label="Shade the board by"/);
    expect(html).not.toContain('role="toolbar"');
    expect(count(html, 'aria-pressed="false"')).toBe(4);
  });

  it("keeps the explanation off the face and out of a native title", () => {
    // The face is given over to live numbers; what the mode means is on a `Tooltip`, which a
    // keyboard reaches as well as a pointer. A `title` would show to neither.
    const html = bar(SHADE_MODES);
    expect(html).not.toContain("title=");
    expect(html).not.toContain("Dollars spent across every run of this ticket");
  });
});

describe("the button figures", () => {
  const spent = [
    node({ id: 1, costUsd: 2, minutes: 30, tokens: 100_000 }),
    node({ id: 2, costUsd: 8, minutes: 90, tokens: 900_000 }),
  ];

  it("reports the sum and the single worst ticket, in the metric's own units", () => {
    // Each figure carries its unit boxed with it, the way a ticket page prints one.
    expect(metrics(bar(SHADE_MODES, spent))).toEqual(
      expect.arrayContaining([
        ["$", "10.00"],
        ["$", "8.00"],
      ]),
    );
  });

  it("labels each figure rather than trailing a word off one of them", () => {
    // The pair is read down a column, so the two figures have to start at the same place. A
    // suffix on the max put the one asymmetry in the pair on the end that has to line up.
    const html = bar(SHADE_MODES, spent);
    expect(html).toContain('factory-board__mode-key">Total<');
    expect(html).toContain('factory-board__mode-key">Max<');
    expect(html).not.toContain(" max");
  });

  it("gives the max its own unit, so neither line leans on the other for what it measures", () => {
    // `900k` under `1.0M tok` was borrowing the total's noun. Both are the standalone form now.
    expect(metrics(bar(SHADE_MODES, spent))).toEqual(
      expect.arrayContaining([
        ["1.0M", "tok"],
        ["900k", "tok"],
      ]),
    );
  });

  it("draws no issue count beside the label, the face being given over to the sums", () => {
    // The badge said the same thing on all four buttons — how many tickets are selected, which
    // the stats panel beside them already carries — and it took the width the figures needed.
    expect(bar(SHADE_MODES, spent)).not.toContain("factory-board__mode-count");
  });

  it("stacks the total over the max, so four buttons make two readable columns", () => {
    // Written in the order they are read: the sum ranks the buttons against each other, the max
    // under it says whether that sum is one runaway or a hundred ordinary tickets.
    const html = bar(SHADE_MODES, spent);
    expect(html.indexOf("factory-board__mode-total")).toBeLessThan(
      html.indexOf("factory-board__mode-max"),
    );
  });

  it("sizes both lines' boxes together, so a unit's block lands in one place", () => {
    // Gathered from the two strings before anything renders — nothing in the cascade can make one
    // box the size of another when the two are in separate grid rows. Every box of a unit holds
    // room for BOTH figures, so `$10.00` and `$8.00` come out one width.
    const html = bar(SHADE_MODES, spent);
    const held = [...html.matchAll(/<span aria-hidden="true" class="invisible[^"]*">([^<]*)</g)]
      .map((match) => match[1] as string)
      .join(" ");
    // The cost pair, on both of its lines.
    expect(held).toContain("10.00 8.00 10.00 8.00");
    // TIME's hour box is sized against the other line's hour, never against its minutes: the
    // total is `2h` and the max `1h 30m`, so `h` holds 2 and 1 while `m` holds 30 alone.
    expect(held).toContain("2 1 2 1 30");
  });

  it("narrows to whatever set it is given, which is how the focus reaches the rail", () => {
    // The page hands it the focused tickets. Nothing about the bar knows that — it only has to
    // be true that a smaller set produces smaller figures.
    const whole = bar(SHADE_MODES, spent);
    const one = bar(SHADE_MODES, [spent[1] as IssueNode]);
    expect(metrics(whole)).toContainEqual(["$", "10.00"]);
    expect(metrics(one)).not.toContainEqual(["$", "10.00"]);
    expect(metrics(one)).toContainEqual(["$", "8.00"]);
  });

  it("says so plainly when the selection holds nothing", () => {
    const html = bar(SHADE_MODES, []);
    expect(html).toContain("Nothing selected");
    expect(html).not.toContain("factory-board__mode-total");
  });
});

describe("HeatLegend", () => {
  it("names one band per shade the board can paint", () => {
    const html = renderToString(<HeatLegend metric="retry" />);
    expect(count(html, 'class="factory-board__legend-swatch"')).toBe(4);
    for (const shade of ["heat-0", "heat-1", "heat-2", "heat-3"]) {
      expect(html).toContain(`data-look="${shade}"`);
    }
    // The top band is open-ended: three retries and thirty are the same colour.
    expect(html).toContain("3+");
  });

  it("names the breaks in the active metric's units, not in retries", () => {
    const html = renderToString(<HeatLegend metric="cost" />);
    expect(html).toContain("$5.00+");
    expect(html).toContain('factory-board__legend-caption">Cost<');
  });

  it("writes the breaks as plain text, alone among the board's figures", () => {
    // The key is a legend, not a readout: four ranges on one strip, in a label whose whole job is
    // to ellipsise rather than push the buttons about — which boxed units, being atomic, cannot
    // do. The gutter and the metric buttons keep theirs.
    expect(metrics(renderToString(<HeatLegend metric="cost" />))).toEqual([]);
  });

  it("follows the thresholds, so the key cannot outlive a setting", () => {
    const html = renderToString(
      <HeatLegend
        metric="retry"
        thresholds={{ ...DEFAULT_THRESHOLDS, retry: { lower: 2, upper: 8 } }}
      />,
    );
    expect(html).toContain("Under 2");
    expect(html).toContain("8+");
  });

  it("says what it is waiting for when no metric is on, over an intact ramp", () => {
    // The four bands are still RENDERED, merely made invisible, so the box's height is set by
    // the same content in both states. Anything that removed them would let the strip change
    // height on the first hover and shift the whole page under the reader.
    const html = renderToString(<HeatLegend metric={null} />);
    expect(html).toContain('data-on="false"');
    expect(html).toContain("Select a metric");
    expect(count(html, 'class="factory-board__legend-swatch"')).toBe(4);
  });

  it("draws the same number of bands whether or not a metric is on", () => {
    // The assertion the height depends on, stated as the invariant it is.
    const off = renderToString(<HeatLegend metric={null} />);
    const on = renderToString(<HeatLegend metric="cost" />);
    expect(count(off, "factory-board__legend-band")).toBe(count(on, "factory-board__legend-band"));
    expect(count(off, "factory-board__legend-caption")).toBe(
      count(on, "factory-board__legend-caption"),
    );
    // And only the off state carries the placeholder laid over them.
    expect(on).not.toContain("factory-board__legend-idle");
  });
});

describe("AppliedRow", () => {
  const PROJECTS = [
    { id: 1, name: "widgets" },
    { id: 2, name: "payments-api" },
  ];
  const row = (
    applied: Partial<Pick<Parameters<typeof AppliedRow>[0], "metric" | "focus" | "laneSort">> = {},
  ) =>
    renderToString(
      <MemoryRouter>
        <AppliedRow
          id="applied"
          metric={applied.metric ?? null}
          focus={applied.focus ?? null}
          laneSort={applied.laneSort ?? null}
          projects={PROJECTS}
          onClearMetric={() => {}}
          onClearFocus={() => {}}
          onClearSort={() => {}}
          onReset={() => {}}
        />
      </MemoryRouter>,
    );
  const dead = 'aria-disabled="true"';

  it("says in words that nothing is applied, and Reset points at those words", () => {
    const html = row();
    expect(html).toMatch(/id="applied"[^>]*>.*Nothing is applied to the board\./s);
    expect(html).toMatch(/aria-disabled="true"[^>]*aria-describedby="applied"/);
  });

  it("keeps Reset mounted, focusable and inert when nothing is applied, so nothing moves", () => {
    // `disabled` drops a button out of the tab order and no browser shows its title, so the
    // reason it does nothing would live where nobody could read it.
    const html = row();
    expect(html).toContain("Reset view");
    expect(html).not.toMatch(/\sdisabled=""/);
    expect(html).not.toContain("title=");
    expect(count(html, dead)).toBe(1);
  });

  it("wakes Reset for any one of the three, since it is the way out of all of them", () => {
    for (const applied of [
      { metric: "cost" as const },
      { focus: { kind: "lane", lane: "gate" } as const },
      { laneSort: { lane: "error", dir: "desc" } as const },
    ]) {
      expect(count(row(applied), dead)).toBe(0);
    }
  });

  it("names each applied thing as a tag that carries its own clear", () => {
    const html = row({
      metric: "cost",
      focus: { kind: "cell", lane: "error", projectId: 1 },
      laneSort: { lane: "error", dir: "desc" },
    });
    expect(html).toContain("Metric: <!-- -->Cost");
    expect(html).toContain("Focus: <!-- -->widgets / Error");
    expect(html).toContain("Sort: <!-- -->Error");
    expect(html).toContain('aria-label="most first"');
    for (const clear of ["Clear metric", "Clear focus", "Clear sort"]) {
      expect(html).toContain(`aria-label="${clear}"`);
    }
    expect(html).not.toContain("Nothing is applied");
  });

  it("writes every label in sentence case", () => {
    const html = row({ metric: "retry", focus: { kind: "project", projectId: 2 } });
    for (const shout of ["METRICS", "FOCUS", "RESET"]) expect(html).not.toContain(shout);
  });
});

describe("DENSITIES", () => {
  it("offers three sizes, largest last, and opens on the middle one", () => {
    // The default is a judgement rather than an index: Medium still reads as a square while
    // fitting a tall factory in the frame. `DEFAULT_DENSITY` has to follow the array when a size
    // is added to it, and `factory-board.css` declares this pair as its own `--fb-*` fallback.
    expect(DENSITIES.map((density) => density.label)).toEqual(["Small", "Medium", "Large"]);
    expect(DENSITIES[DEFAULT_DENSITY]?.label).toBe("Medium");
    expect(DENSITIES[DEFAULT_DENSITY]?.node).toBe(6);
  });

  it("grows the square and never shrinks it, so the labels are in the order they claim", () => {
    const nodes = DENSITIES.map((density) => density.node);
    expect(nodes).toEqual([...nodes].sort((a, b) => a - b));
  });
});
