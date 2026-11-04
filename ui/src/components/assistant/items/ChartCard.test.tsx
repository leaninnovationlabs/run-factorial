// @vitest-environment happy-dom
/** A chart card: the picture, and View data, the same numbers as a table. */

import { afterEach, describe, expect, test } from "vitest";
import type { ChartPayload } from "../../../api/types";
import { byText, click, type Mounted } from "../../../testDom";
import { mountInAssistant } from "../testFakes";
import { ChartCard } from "./ChartCard";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
});

const COST: ChartPayload = {
  kind: "line",
  title: "Cost per day",
  unit: "USD",
  x: ["Oct 1", "Oct 2", "Oct 3"],
  series: [
    { name: "api", values: [1.5, 2.25, 0.5] },
    { name: "web", values: [0.75, 1, 3] },
  ],
  source: "factorial_costs_get",
};

async function render(payload: ChartPayload) {
  const mounted = await mountInAssistant(<ChartCard payload={payload} />);
  view = mounted.view;
  return mounted.view.container;
}

describe("ChartCard", () => {
  test("ChartCard draws one point per value and View data shows the same numbers", async () => {
    const root = await render(COST);
    expect(root.textContent).toContain("Cost per day");
    expect(root.textContent).toContain("USD");
    expect(root.textContent).toContain("factorial_costs_get");
    const points = [...root.querySelectorAll("[data-point]")];
    expect(points).toHaveLength(6);
    const drawn = points.map((point) => point.querySelector("title")?.textContent);
    expect(root.querySelector("table")).toBeNull();

    await click(byText(root, "button", "View data"));
    const rows = [...root.querySelectorAll("tbody tr")].map((row) =>
      [...row.querySelectorAll("td")].map((cell) => cell.textContent),
    );
    expect(rows).toEqual([
      ["Oct 1", "$1.50", "$0.75"],
      ["Oct 2", "$2.25", "$1.00"],
      ["Oct 3", "$0.50", "$3.00"],
    ]);
    for (const [label, ...values] of rows) {
      values.forEach((value, index) => {
        expect(drawn).toContain(`${COST.series[index]?.name}, ${label}: ${value}`);
      });
    }
    await click(byText(root, "button", "Hide data"));
    expect(root.querySelector("table")).toBeNull();
  });

  test("a grouped bar chart draws one bar per value, and one series needs no legend", async () => {
    let root = await render({ ...COST, kind: "grouped_bar" });
    expect(root.querySelectorAll("[data-bar]")).toHaveLength(6);
    expect(root.querySelector("[data-legend]")?.textContent).toContain("api");
    await view?.unmount();

    root = await render({
      ...COST,
      kind: "bar",
      unit: "tickets",
      series: [{ name: "done", values: [3, 4, 1200] }],
    });
    expect(root.querySelectorAll("[data-bar]")).toHaveLength(3);
    expect(root.querySelector("[data-legend]")).toBeNull();
    expect(root.querySelector("[data-bar] title")?.textContent).toBe("done, Oct 1: 3");
    expect(root.querySelectorAll("[data-bar] title")[2]?.textContent).toBe("done, Oct 3: 1,200");
  });

  test("a payload that is not a chart draws nothing", async () => {
    const root = await render({ ...COST, series: [{ name: "api", values: [1] }] });
    expect(root.querySelector("svg")).toBeNull();
  });
});
