/**
 * The stacked bar is readable with no pointer: its name says every part, and the row under it
 * writes every part's figure — a part worth nothing included.
 */

import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { StackedBar } from "./StackedBar";

const SEGMENTS = [
  { key: "done", look: "outcome-done", label: "Done", value: 46.4, text: "$46.40 · 70%" },
  { key: "open", look: "outcome-open", label: "Open", value: 11.67, text: "$11.67 · 18%" },
  { key: "exited", look: "outcome-exited", label: "Exited", value: 0, text: "$0.00 · 0%" },
];

describe("StackedBar", () => {
  it("names every part in the image's label and writes every figure as text", () => {
    const html = renderToString(<StackedBar label="Spend by outcome" segments={SEGMENTS} />);
    expect(html).toContain(
      'aria-label="Spend by outcome: Done $46.40 · 70%, Open $11.67 · 18%, Exited $0.00 · 0%"',
    );
    expect(html.match(/data-segment=/g)).toHaveLength(2);
    // The bar's part and the legend's swatch both carry the look the hatch rule keys on.
    expect(html.match(/data-chart-look="outcome-done"/g)).toHaveLength(2);
    expect(html).toContain("$0.00 · 0%");
  });
});
