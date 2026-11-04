import { describe, expect, it } from "vitest";
import { CORNER_DIRECTIONS, SELECT_CORNER, selectCornerTriangles } from "./flowSelect";

describe("selectCornerTriangles", () => {
  it("puts a right triangle on each corner of the selection box", () => {
    const box = { x: 10, y: 20, width: 100, height: 50 };
    const [tl, tr, bl, br] = selectCornerTriangles(box);
    const s = SELECT_CORNER;
    const x2 = box.x + box.width;
    const y2 = box.y + box.height;

    expect(tl).toEqual([
      { x: box.x, y: box.y },
      { x: box.x + s, y: box.y },
      { x: box.x, y: box.y + s },
    ]);
    expect(tr).toEqual([
      { x: x2, y: box.y },
      { x: x2 - s, y: box.y },
      { x: x2, y: box.y + s },
    ]);
    expect(bl).toEqual([
      { x: box.x, y: y2 },
      { x: box.x + s, y: y2 },
      { x: box.x, y: y2 - s },
    ]);
    expect(br).toEqual([
      { x: x2, y: y2 },
      { x: x2 - s, y: y2 },
      { x: x2, y: y2 - s },
    ]);
  });
});

describe("CORNER_DIRECTIONS", () => {
  it("points each corner away from the centre of its own box", () => {
    const box = { x: 10, y: 20, width: 100, height: 50 };
    const centre = { x: box.x + box.width / 2, y: box.y + box.height / 2 };

    // Every corner's own vertex is the first point of its triangle. Its direction must have the
    // same signs, or a pulsing reticle would collapse inward on that corner.
    for (const [index, [vertex]] of selectCornerTriangles(box).entries()) {
      expect(CORNER_DIRECTIONS[index]).toEqual({
        x: Math.sign(vertex.x - centre.x),
        y: Math.sign(vertex.y - centre.y),
      });
    }
  });
});
