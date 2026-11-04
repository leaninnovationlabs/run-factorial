/** Leg length of each selection-corner triangle. Tweak this to resize the chrome. */
export const SELECT_CORNER = 6;

export interface SelectBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface Point {
  x: number;
  y: number;
}

export type CornerTriangle = [Point, Point, Point];

/**
 * Four right triangles whose legs sit on the selection rectangle.
 * Order: top-left, top-right, bottom-left, bottom-right.
 */
export function selectCornerTriangles(
  box: SelectBox,
  size: number = SELECT_CORNER,
): [CornerTriangle, CornerTriangle, CornerTriangle, CornerTriangle] {
  const x2 = box.x + box.width;
  const y2 = box.y + box.height;
  return [
    [
      { x: box.x, y: box.y },
      { x: box.x + size, y: box.y },
      { x: box.x, y: box.y + size },
    ],
    [
      { x: x2, y: box.y },
      { x: x2 - size, y: box.y },
      { x: x2, y: box.y + size },
    ],
    [
      { x: box.x, y: y2 },
      { x: box.x + size, y: y2 },
      { x: box.x, y: y2 - size },
    ],
    [
      { x: x2, y: y2 },
      { x: x2 - size, y: y2 },
      { x: x2, y: y2 - size },
    ],
  ];
}

/**
 * The outward diagonal of each corner, in the order `selectCornerTriangles` returns them.
 *
 * The reticle pulses along these, so every corner moves away from the box and the four of them
 * read as one frame breathing rather than as four marks drifting. Kept beside the function that
 * fixes the order — the two must not drift apart, which `flowSelect.test.ts` asserts.
 */
export const CORNER_DIRECTIONS: readonly [Point, Point, Point, Point] = [
  { x: -1, y: -1 },
  { x: 1, y: -1 },
  { x: -1, y: 1 },
  { x: 1, y: 1 },
];

export function trianglePoints(triangle: CornerTriangle): string {
  return triangle.map((point) => `${point.x},${point.y}`).join(" ");
}
