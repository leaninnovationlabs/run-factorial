/**
 * The bar's one promise: its length is its share of the max every row shares, and a zero is a
 * visible mark. A width computed from the wrong max would draw a lie nobody could see.
 */

import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { BarList } from "./BarList";

/** The inline widths of the marks, in document order. */
const widths = (html: string) =>
  [...html.matchAll(/data-(?:bar|segment)="[^"]*"[^>]*style="width:([^;"]+)/g)].map((m) => m[1]);

describe("BarList", () => {
  it("draws every bar as a share of the same max", () => {
    const html = renderToString(
      <BarList
        label="Cost by ticket"
        rows={[
          { key: "a", label: "#7", value: 10, text: "$10.00", look: "outcome-done" },
          { key: "b", label: "#8", value: 5, text: "$5.00", look: "outcome-open" },
        ]}
      />,
    );
    expect(widths(html)).toEqual(["100%", "50%"]);
  });

  it("shares a caller's max across rows and segments alike", () => {
    const html = renderToString(
      <BarList
        label="Where the steps spend"
        max={20}
        rows={[
          {
            key: "a",
            label: "#7",
            value: 10,
            text: "$10.00",
            segments: [
              { key: "plan", look: "series-1", value: 2 },
              { key: "build", look: "series-2", value: 8 },
            ],
          },
          { key: "b", label: "#8", value: 5, text: "$5.00", look: "outcome-done" },
        ]}
      />,
    );
    expect(widths(html)).toEqual(["10%", "40%", "25%"]);
    expect(html.match(/data-chart-look="series-\d"/g)).toHaveLength(2);
  });

  it("still draws a bar worth nothing", () => {
    const html = renderToString(
      <BarList
        label="Cost by ticket"
        rows={[{ key: "a", label: "#7", value: 0, text: "$0.00", look: "outcome-exited" }]}
      />,
    );
    expect(widths(html)).toEqual(["0%"]);
    expect(html).toMatch(/data-bar[^>]*class="[^"]*min-w-0\.5/);
  });
});
