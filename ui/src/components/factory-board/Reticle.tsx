/**
 * The selection reticle: right triangles sitting on the corners of a node group.
 *
 * The geometry is the process graph's, imported rather than reimplemented — `flowSelect.ts` is
 * already the one definition of the corner shape and of the outward diagonal each corner pulses
 * along, and `flowSelect.test.ts` pins the two together. The pulse is shared too
 * (`theme/motion.css`); only the colour and the reach are the board's own.
 *
 * A reticle may draw only SOME of its corners: a row or column focus splits one rectangle's four
 * corners between the two groups at its ends. Which is why the phase clock below matters.
 */

import { useState } from "react";
import { CORNER_DIRECTIONS, selectCornerTriangles, trianglePoints } from "../flow/flowSelect";
import { PULSE_MS } from "../ui/motion";
import type { Corners } from "./focus";

/** How far outside the node group's own box the pips sit, in px. */
export const RETICLE_PAD = 3;

/**
 * One clock for every reticle on the page.
 *
 * The pips describe a single rectangle, so they have to breathe as one — but they are separate
 * elements in separate cells, and CSS starts an animation when its element mounts. Two pips
 * mounting a moment apart (a focus moving from one column to another, where one end persists and
 * the other is new) would then pulse out of step, and the rectangle would visibly come apart.
 * A negative delay starts a late arrival partway through its cycle instead, in step with
 * whatever is already running.
 */
const EPOCH = Date.now();

function phaseDelay(): string {
  return `${-((Date.now() - EPOCH) % PULSE_MS)}ms`;
}

/** Stable keys, in `selectCornerTriangles` order. */
const CORNER_NAMES = ["tl", "tr", "bl", "br"] as const;

/**
 * One pip.
 *
 * The phase is per CORNER, not per reticle, because corners outlive reticles: narrowing a column
 * focus to one of its node groups keeps that group's existing pips mounted and adds the two the
 * other end used to hold. Those arrivals are new elements inside an old parent, so a delay
 * computed when the parent mounted would put them out of step — which is precisely the drift
 * this prevents. Computed once, in a lazy initialiser: recomputing on a re-render would restart
 * the animation and cause the same problem from the other direction.
 */
function Corner({ points, direction }: { points: string; direction: { x: number; y: number } }) {
  const [delay] = useState(phaseDelay);
  return (
    <polygon
      className="factory-board__reticle-corner"
      points={points}
      style={{
        // Each corner carries the sign of its own diagonal, so one keyframe drives all four
        // outward together and back — the frame breathes instead of four marks drifting.
        ["--reticle-x" as string]: direction.x,
        ["--reticle-y" as string]: direction.y,
        ["--fb-reticle-delay" as string]: delay,
      }}
    />
  );
}

export function Reticle({
  width,
  height,
  corners,
}: {
  width: number;
  height: number;
  corners: Corners;
}) {
  const box = { x: 0, y: 0, width: width + RETICLE_PAD * 2, height: height + RETICLE_PAD * 2 };

  return (
    <svg
      className="factory-board__reticle"
      width={box.width}
      height={box.height}
      style={{ left: `${-RETICLE_PAD}px`, top: `${-RETICLE_PAD}px` }}
      aria-hidden="true"
    >
      {selectCornerTriangles(box).map((triangle, index) =>
        corners[index] ? (
          <Corner
            key={CORNER_NAMES[index]}
            points={trianglePoints(triangle)}
            direction={CORNER_DIRECTIONS[index] ?? { x: 0, y: 0 }}
          />
        ) : null,
      )}
    </svg>
  );
}
