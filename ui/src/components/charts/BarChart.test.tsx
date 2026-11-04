// @vitest-environment happy-dom
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, test } from "vitest";
import { BarChart, barPath } from "./BarChart";

describe("BarChart", () => {
  test("draws one bar per value in each label's group, no wider than 24", () => {
    const host = document.createElement("div");
    host.innerHTML = renderToStaticMarkup(
      <BarChart
        label="Minutes per step"
        x={["plan", "build"]}
        series={[
          { name: "api", values: [4, 9] },
          { name: "web", values: [2, 0] },
          { name: "cli", values: [1, 1] },
        ]}
        unit="minutes"
        format={(value) => `${value} min`}
      />,
    );
    const bars = [...host.querySelectorAll("[data-bar]")];
    expect(bars).toHaveLength(6);
    expect(bars[1]?.querySelector("title")?.textContent).toBe("api, build: 9 min");
    expect(host.textContent).toContain("minutes");
  });

  test("rounds a bar at its end and keeps it square at the baseline, either side of zero", () => {
    expect(barPath(10, 20, 100, 40)).toBe("M10,100 V44 Q10,40 14,40 H26 Q30,40 30,44 V100 Z");
    expect(barPath(10, 20, 100, 160)).toBe("M10,100 V156 Q10,160 14,160 H26 Q30,160 30,156 V100 Z");
  });
});
