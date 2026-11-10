/**
 * The shared vocabulary's markup rules — the ones a reader would notice if they broke.
 */

import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { NO_VALUE } from "../../format";
import { Metric } from "./Metric";

/** Each metric box's two halves, in the order they are written.
 *
 *  Anchored on the `data-metric-*` attributes rather than on the utility classes beside them:
 *  those are the box's own styling and change with it, and a helper that reads them turns every
 *  restyle into a suite-wide failure that says nothing about what actually broke. */
function boxes(html: string): string[][] {
  return html
    .split("<span data-metric-box")
    .slice(1)
    .map((chunk) => {
      const unit = chunk.match(/data-metric-unit[^>]*>([^<]*)</)?.[1] ?? "";
      const value = chunk.match(/data-metric-value[^>]*>([^<]*)</)?.[1] ?? "";
      // The visible figure shares its cell with the invisible sizers, so it is addressed by
      // `data-metric-value` rather than by position. Which half comes first is the box's own
      // statement about where the unit is written — `$0.94` against `57m`.
      return chunk.indexOf("data-metric-unit") < chunk.indexOf("data-metric-value")
        ? [unit, value]
        : [value, unit];
    });
}

/** The opening tag of every unit half — the one part of a metric with a ground of its own. */
function unitTags(html: string): string[] {
  return html.match(/<span data-metric-unit[^>]*>/g) ?? [];
}

/** The invisible figures a box is holding room for, in the order they are written. */
function sizers(html: string): string[] {
  return [...html.matchAll(/<span aria-hidden="true" class="invisible[^"]*">([^<]*)</g)].map(
    (match) => match[1] as string,
  );
}

/** What each unit half says. */
function filled(html: string): string[] {
  return [...html.matchAll(/<span data-metric-unit[^>]*>([^<]*)<\/span>/g)].map(
    (match) => match[1] as string,
  );
}

describe("Metric", () => {
  it("writes the currency mark ahead of its figure, in one box", () => {
    const html = renderToString(<Metric>$0.94</Metric>);
    expect(boxes(html)).toEqual([["$", "0.94"]]);
  });

  it("gives each figure of a duration its own box", () => {
    expect(boxes(renderToString(<Metric>57m 51s</Metric>))).toEqual([
      ["57", "m"],
      ["51", "s"],
    ]);
  });

  it("fills the unit half and leaves the figure half clear", () => {
    const html = renderToString(<Metric>41.9K in / 9.2K out</Metric>);
    expect(filled(html)).toEqual(["in", "out"]);
    // The figure is in the same box, outside the fill.
    expect(html).toContain("41.9K");
  });

  it("draws the fill in a quiet neutral grey, with default type to sit on it", () => {
    // The unit is the one part of a metric that does not inherit the ambient colour, because it
    // is the one part with a ground of its own: the metric token, the default button's grey in
    // light mode. The near-black it used to be out-shouted the status beside it.
    const [unit] = unitTags(renderToString(<Metric>41.9K in</Metric>));
    expect(unit).toContain("bg-metric");
    expect(unit).toContain("text-default");
    // Bold, because a unit is one or three characters at the smallest size on the page.
    expect(unit).toContain("font-bold");
  });

  it("squares a one-character unit and lets a longer one grow", () => {
    // `min-w` is the square: a single mark is centred in a box as wide as it is tall, and `out`
    // pushes past it into a rectangle rather than being squeezed.
    expect(renderToString(<Metric>$0.94</Metric>)).toContain("min-w-[");
  });

  it("keeps what is written around a metric", () => {
    expect(renderToString(<Metric>&lt;$0.01</Metric>)).toContain("&lt;");
    expect(renderToString(<Metric>41.9K in / 9.2K out</Metric>)).toContain(" / ");
  });

  it("boxes nothing when there is no metric to box", () => {
    const html = renderToString(<Metric>{NO_VALUE}</Metric>);
    expect(boxes(html)).toEqual([]);
    expect(html).toContain(`>${NO_VALUE}<`);
  });

  it("shortens the one unit too long for its box, and writes the rest as they came", () => {
    // `retries` is four times the figure it follows and the box is the size of a `$`, so it is
    // written as a count's "times" — the same trade `tok` already makes. Nothing else is rewritten.
    expect(filled(renderToString(<Metric>3 retries</Metric>))).toEqual(["x"]);
    expect(filled(renderToString(<Metric>1 retry</Metric>))).toEqual(["x"]);
    expect(filled(renderToString(<Metric>120k tok</Metric>))).toEqual(["tok"]);
    expect(filled(renderToString(<Metric>57m 51s</Metric>))).toEqual(["m", "s"]);
  });

  it("lays every figure of a set into the box, so the browser sizes it to the widest", () => {
    // What makes two boxes stack: `$8.00` drawn in the room `$10.00` needs comes out the same
    // size, so the filled halves land on the same pixel instead of a digit apart. A character
    // count could not do it — `1.0M` and `900k` are both four characters and one is wider — so
    // every candidate is rendered and the widest ACTUAL rendering wins.
    const html = renderToString(<Metric figureSizers={{ $: ["10.00", "8.00"] }}>$8.00</Metric>);
    expect(sizers(html)).toEqual(["10.00", "8.00"]);
    // Invisible, not hidden: taking up space is the only reason a sizer is there.
    expect(html).toMatch(/aria-hidden="true" class="invisible/);
    // And exactly one of them is the figure a reader sees.
    expect(boxes(html)).toEqual([["$", "8.00"]]);
  });

  it("sizes a single retry in the room the plural's figures need", () => {
    // `1 retry` under `12 retries`: one count drawn with one `×`, gathered under the plural.
    const html = renderToString(<Metric figureSizers={{ retries: ["12", "1"] }}>1 retry</Metric>);
    expect(sizers(html)).toEqual(["12", "1"]);
  });

  it("hugs its figure when it is not one of a set, which is every other use", () => {
    expect(sizers(renderToString(<Metric>$8.00</Metric>))).toEqual([]);
    // A unit the caller did not collect is left to itself too.
    expect(sizers(renderToString(<Metric figureSizers={{ h: ["2"] }}>$8.00</Metric>))).toEqual([]);
  });

  it("right-aligns the figure in the room it was given", () => {
    // A column of stacked boxes is read down the digits, so the padding falls on the left of a
    // short figure rather than the right.
    const [figure] = renderToString(
      <Metric figureSizers={{ $: ["10.00", "8.00"] }}>$8.00</Metric>,
    ).match(/<span data-metric-figure[^>]*>/g) ?? [""];
    expect(figure).toContain("justify-items-end");
  });

  it("names the box's parts, so a layout can line two of them up", () => {
    // The board's metric buttons stretch the figure half to give a column of boxes one width.
    // Reaching those halves by utility class would put this alignment at the mercy of a restyle.
    const html = renderToString(<Metric>$0.94</Metric>);
    expect(html).toContain("data-metric-box");
    expect(html).toContain("data-metric-figure");
    expect(html).toContain("data-metric-unit");
  });
});
