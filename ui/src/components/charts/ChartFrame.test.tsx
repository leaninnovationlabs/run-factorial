import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, test } from "vitest";
import { ChartFrame, SERIES_COLOURS, seriesColour, ticks } from "./ChartFrame";

describe("ticks", () => {
  test("are round numbers from zero that reach past the largest value", () => {
    expect(ticks(0.5, 3)).toEqual([0, 1, 2, 3]);
    expect(ticks(3, 1200)).toEqual([0, 500, 1000, 1500]);
    expect(ticks(-2, 6)).toEqual([-2, 0, 2, 4, 6]);
    expect(ticks(0, 0)).toEqual([0, 1]);
    // From zero however far above it the values sit: a bar's length is its value.
    expect(ticks(100, 110)).toEqual([0, 50, 100, 150]);
  });
});

describe("ChartFrame", () => {
  test("names each series in a legend once there are two, and says where the numbers came from", () => {
    const one = renderToStaticMarkup(
      <ChartFrame title="Cost" series={[{ name: "api", values: [1] }]} mark="bar" footer="From x">
        <svg />
      </ChartFrame>,
    );
    expect(one).not.toContain("data-legend");
    expect(one).toContain("From x");
    const two = renderToStaticMarkup(
      <ChartFrame
        title="Cost"
        series={[
          { name: "api", values: [1] },
          { name: "web", values: [2] },
        ]}
        mark="line"
      >
        <svg />
      </ChartFrame>,
    );
    expect(two).toContain("data-legend");
    expect(two).toContain(SERIES_COLOURS[1]);
  });

  test("gives each of six series its own token colour", () => {
    const colours = [0, 1, 2, 3, 4, 5].map(seriesColour);
    expect(new Set(colours).size).toBe(6);
    for (const colour of colours) expect(colour).toMatch(/^var\(--ds-chart-[a-z]+-bold\)$/);
  });
});
