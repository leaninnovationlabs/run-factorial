// @vitest-environment happy-dom
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, test } from "vitest";
import { LineChart } from "./LineChart";

function render(values: number[][]) {
  const host = document.createElement("div");
  host.innerHTML = renderToStaticMarkup(
    <LineChart
      label="Cost per day"
      x={values[0]?.map((_, index) => `Oct ${index + 1}`) ?? []}
      series={values.map((line, index) => ({ name: `s${index}`, values: line }))}
      unit="USD"
      format={(value) => `$${value}`}
    />,
  );
  return host;
}

describe("LineChart", () => {
  test("draws one line per series through one point per value, under the unit", () => {
    const host = render([
      [1, 2, 3],
      [3, 2, 1],
    ]);
    expect(host.querySelectorAll("[data-series] path")).toHaveLength(2);
    expect(host.querySelectorAll("[data-point]")).toHaveLength(6);
    expect(host.querySelector("svg")?.getAttribute("aria-label")).toBe("Cost per day");
    expect(host.textContent).toContain("USD");
    expect(host.querySelector("[data-point] title")?.textContent).toBe("s0, Oct 1: $1");
  });

  test("puts a higher value higher, and a lone value in the middle", () => {
    const host = render([[1, 3]]);
    const [low, high] = [...host.querySelectorAll("[data-point] circle:last-child")].map((dot) =>
      Number(dot.getAttribute("cy")),
    );
    expect(high).toBeLessThan(low ?? 0);
    const lone = render([[5]]);
    expect(Number(lone.querySelector("[data-point] circle")?.getAttribute("cx"))).toBe(
      64 + (640 - 64 - 16) / 2,
    );
  });
});
