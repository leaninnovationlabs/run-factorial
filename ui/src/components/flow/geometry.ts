/**
 * The process graph's layout, in pixels: where a node sits, how big it draws, where its label
 * wraps and where its edges run. Pure, so it is tested without a DOM; `FlowGraph` draws what this
 * places.
 */

import type { CSSProperties } from "react";
import { CORNER_DIRECTIONS, type Point, type SelectBox } from "./flowSelect";
import type {
  ProcessGraph,
  ProcessGraphEdgeKind,
  ProcessGraphKind,
  ProcessGraphNode,
} from "./processGraph";

export const COL_W = 104;
export const PAD_X = 36;
export const PAD_Y = 56;
const NODE = 18;
const GATE = 24;
export const LABEL_DY = 28;
/**
 * The attempt count sits below the node label by the same distance the label sits below the node
 * itself, so the three read as one stack rather than as a label with something stuck under it.
 * Derived rather than typed out: change `LABEL_DY` and the spacing stays even.
 *
 * The selection frame allows for this line on every node, so a frame is the same height whether
 * the node has run or not.
 */
export const BADGE_DY = LABEL_DY - NODE / 2;
const SELECT_PAD_X = 12;
const SELECT_PAD_Y = 10;
const CHAR_W = 6.4;

/**
 * A node label wraps rather than running into its neighbours.
 *
 * `COL_W` is the distance between two node centres, so a label centred on one reaches half a
 * column each way before it meets the next label doing the same. One character of gutter keeps
 * two full-width labels from touching.
 *
 * Two lines, then truncation. A third line would push the attempt count into the row below, and
 * a step name that needs three lines is not being read off a diagram anyway — the full name stays
 * reachable as the `<title>` on the label.
 */
const LABEL_MAX_CHARS = Math.floor(COL_W / CHAR_W) - 1;
const LABEL_MAX_LINES = 2;
/** Matches `.flow-small-label`'s 0.625rem at line-height 1.2. */
export const LABEL_LINE_H = 12;

/** Cut `line` to fit, ending in an ellipsis — used only where something was dropped. */
function ellipsize(line: string): string {
  return line.length < LABEL_MAX_CHARS ? `${line}…` : `${line.slice(0, LABEL_MAX_CHARS - 1)}…`;
}

/**
 * One label as the lines it draws on.
 *
 * Breaks after `_`, `-` or a space and keeps the separator on the line it ended, so `spec_review_
 * gate` reads as the snake_case name it is rather than losing the underscore at the fold. A single
 * token longer than one line has no break to take and is cut instead.
 */
export function wrapLabel(label: string): string[] {
  if (label.length <= LABEL_MAX_CHARS) return [label];

  const lines: string[] = [];
  let line = "";
  for (const part of label.split(/(?<=[_\- ])/)) {
    if (line && (line + part).trimEnd().length > LABEL_MAX_CHARS) {
      lines.push(line.trimEnd());
      line = part;
    } else {
      line += part;
    }
  }
  if (line) lines.push(line.trimEnd());

  if (lines.length > LABEL_MAX_LINES) {
    const kept = lines.slice(0, LABEL_MAX_LINES);
    return [...kept.slice(0, -1), ellipsize(kept[kept.length - 1] ?? "")];
  }
  // A line with no break to take is still too wide; cut it.
  return lines.map((one) => (one.length > LABEL_MAX_CHARS ? ellipsize(one) : one));
}

/**
 * How much larger the current step and the selected node draw than the rest.
 *
 * The two conditions do not compound: a node that is both takes the same emphasis once, because
 * a node at 1.74x reads as a different kind of thing rather than as an emphasised one.
 *
 * The shape itself is drawn at its base size and scaled to this by CSS, so the growth can ease
 * (`--flow-resize-duration`); the number is still needed here because the selection frame, the
 * badge and the edge trim all have to reckon with the size the node ends up at.
 */
export const EMPHASIS = 1.32;

/** Lift of a curved edge: a floor, plus a share of the horizontal distance it travels. */
const ARC_LIFT = 36;
const ARC_LIFT_PER_PX = 0.13;

/**
 * The head-and-shoulders glyph above a human gate: its height, and its clearance from the node.
 *
 * The clearance reads the stack's rhythm upwards: the glyph's top sits `BADGE_DY` above the node's
 * edge, the same step by which the attempt count sits below the label. So the gap above a gate is
 * the gap below it, and the glyph reads as part of the stack rather than as something wedged into
 * the diamond.
 */
const PERSON_H = 9.6;
export const PERSON_GAP = BADGE_DY - PERSON_H;

/** Clearance kept above the tallest arc when the viewBox grows to fit it. */
export const ARC_MARGIN = 4;

export function nodeSize(kind: ProcessGraphKind, emphasis = 1): number {
  return (kind === "human_gate" ? GATE : NODE) * emphasis;
}

/** How far above its centre a node's chrome reaches — the shape, plus a gate's person icon. */
function nodeTop(kind: ProcessGraphKind, size: number): number {
  return size / 2 + (kind === "human_gate" ? PERSON_GAP + PERSON_H : 0);
}

export function selectBox(
  x: number,
  y: number,
  label: string,
  kind: ProcessGraphKind,
  size: number,
): SelectBox {
  const top = nodeTop(kind, size);
  const lines = wrapLabel(label);
  const widest = lines.reduce((max, line) => Math.max(max, line.length), 0);
  const width = Math.max(size + SELECT_PAD_X * 2, widest * CHAR_W + SELECT_PAD_X * 2);
  // The frame reaches past every line the label takes, so a wrapped node is framed the way a
  // one-line node is rather than having its second line hang outside.
  const height = top + labelDrop(lines) + BADGE_DY + 4 + SELECT_PAD_Y * 2;
  return {
    x: x - width / 2,
    y: y - top - SELECT_PAD_Y,
    width,
    height,
  };
}

/** How far below a node's centre its label's LAST line sits. */
export function labelDrop(lines: readonly string[]): number {
  return LABEL_DY + (lines.length - 1) * LABEL_LINE_H;
}

export function point(node: ProcessGraphNode): Point {
  return { x: PAD_X + node.column * COL_W, y: PAD_Y };
}

/** The topmost point any arcing edge in the graph reaches, or 0 when none climbs above the rows. */
export function arcCeiling(graph: ProcessGraph, byId: Map<string, ProcessGraphNode>): number {
  let ceiling = 0;
  for (const edge of graph.edges) {
    const fromNode = byId.get(edge.from);
    const toNode = byId.get(edge.to);
    if (!fromNode || !toNode) continue;
    const from = point(fromNode);
    const to = point(toNode);
    if (isStraight(from, to, edge.kind)) continue;
    ceiling = Math.min(ceiling, arcPeak(from, to));
  }
  return ceiling;
}

/** Each reticle corner pulses along its own outward diagonal; `theme/motion.css` reads these as
 *  signs. */
export function reticleDirection(index: number): CSSProperties {
  const direction = CORNER_DIRECTIONS[index];
  return {
    "--reticle-x": direction?.x ?? 0,
    "--reticle-y": direction?.y ?? 0,
  } as CSSProperties;
}

/** An edge along one row, left to right, is drawn as a straight line; everything else arcs. */
function isStraight(from: Point, to: Point, kind: ProcessGraphEdgeKind): boolean {
  return kind === "next" && from.y === to.y && to.x > from.x;
}

/** The highest point an arcing edge reaches. Above the rows, and further above the longer it is. */
function arcPeak(from: Point, to: Point): number {
  return Math.min(from.y, to.y) - (ARC_LIFT + Math.abs(to.x - from.x) * ARC_LIFT_PER_PX);
}

/** `fromHalf` / `toHalf` are the endpoints' own half-sizes, so an edge stops at the shape it
 *  touches rather than at a nominal one — a gate and an emphasised node are both wider. */
export function edgePath(
  from: Point,
  to: Point,
  kind: ProcessGraphEdgeKind,
  fromHalf: number,
  toHalf: number,
): string {
  if (isStraight(from, to, kind)) {
    return `M ${from.x + fromHalf} ${from.y} L ${to.x - toHalf} ${to.y}`;
  }
  const midX = (from.x + to.x) / 2;
  return `M ${from.x} ${from.y - fromHalf} Q ${midX} ${arcPeak(from, to)} ${to.x} ${to.y - toHalf}`;
}
