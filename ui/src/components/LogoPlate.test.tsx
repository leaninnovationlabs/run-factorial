/**
 * The mark is redrawn inline so its parts can animate, and inline means a second copy of the
 * geometry. This is the check that the copy has not drifted from `logo.svg`, which the rail still
 * shows: both are rasterised onto the 184-unit grid and compared pixel for pixel.
 */

import { readFileSync } from "node:fs";
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { LogoPlate, MARK } from "./LogoPlate";

type Box = { x: number; y: number; w: number; h: number };

/** Which of the 184×184 cells a set of boxes paints, as a string a mismatch can diff. */
function raster(boxes: Box[]): string {
  const rows: string[] = [];
  for (let y = 0; y < 184; y++) {
    let row = "";
    for (let x = 0; x < 184; x++) {
      row += boxes.some((b) => x >= b.x && x < b.x + b.w && y >= b.y && y < b.y + b.h) ? "#" : ".";
    }
    rows.push(row);
  }
  return rows.join("\n");
}

/** Every white rect in the file, as written. The first rect is the black plate and is skipped:
 *  on the sign-in page the panel is the plate. */
function fileBoxes(): Box[] {
  const svg = readFileSync(new URL("../../logo.svg", import.meta.url), "utf8");
  return [
    ...svg.matchAll(/<rect x="(\d+)" y="(\d+)" width="(\d+)" height="(\d+)" fill="#ffffff"/g),
  ].map((m) => ({ x: Number(m[1]), y: Number(m[2]), w: Number(m[3]), h: Number(m[4]) }));
}

/** A stroked rect covers a band `stroke/2` either side of its edge. */
function markBoxes(): Box[] {
  return MARK.flatMap((s): Box[] => {
    if (s.kind === "tile") return [{ x: s.x, y: s.y, w: s.w, h: s.h }];
    const half = s.stroke / 2;
    const outer = { x: s.x - half, y: s.y - half, w: s.w + s.stroke, h: s.h + s.stroke };
    return [
      { x: outer.x, y: outer.y, w: outer.w, h: s.stroke },
      { x: outer.x, y: outer.y + outer.h - s.stroke, w: outer.w, h: s.stroke },
      { x: outer.x, y: outer.y, w: s.stroke, h: outer.h },
      { x: outer.x + outer.w - s.stroke, y: outer.y, w: s.stroke, h: outer.h },
    ];
  });
}

describe("LogoPlate", () => {
  it("paints the same cells as logo.svg", () => {
    expect(raster(markBoxes())).toEqual(raster(fileBoxes()));
  });

  it("sets the exclamation dot last", () => {
    const last = MARK[MARK.length - 1];
    expect(last).toMatchObject({ kind: "tile", x: 36, y: 140, w: 32, h: 32 });
  });

  it("names the mark for a screen reader and writes the name beneath it", () => {
    const html = renderToString(<LogoPlate />);
    expect(html).toContain('aria-label="Factorial"');
    expect(html).toContain(">Factorial<");
  });
});
