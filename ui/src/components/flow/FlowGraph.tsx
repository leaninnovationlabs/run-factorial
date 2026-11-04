import { type CSSProperties, useId } from "react";
import type { NodeTone } from "../../status";
import { cx } from "../ui";
import { selectCornerTriangles, trianglePoints } from "./flowSelect";
import {
  ARC_MARGIN,
  arcCeiling,
  BADGE_DY,
  COL_W,
  EMPHASIS,
  edgePath,
  LABEL_DY,
  LABEL_LINE_H,
  labelDrop,
  nodeSize,
  PAD_X,
  PAD_Y,
  PERSON_GAP,
  point,
  reticleDirection,
  selectBox,
  wrapLabel,
} from "./geometry";
import type { ProcessGraph, ProcessGraphKind, ProcessGraphNode } from "./processGraph";

function nodeShape(kind: ProcessGraphKind, x: number, y: number, size: number, className: string) {
  const half = size / 2;
  if (kind === "entry" || kind === "terminal") {
    return <circle className={className} cx={x} cy={y} r={half} />;
  }
  if (kind === "human_gate") {
    return (
      <polygon
        className={className}
        points={`${x},${y - half} ${x + half},${y} ${x},${y + half} ${x - half},${y}`}
      />
    );
  }
  if (kind === "action") {
    return (
      <rect className={className} x={x - half} y={y - half} width={size} height={size} rx={3} />
    );
  }
  if (kind === "check") {
    // A circle at step size: observed, never run — visually lighter than the squares that spawn
    // work, and distinct from the slate entry node. It shares the stage's green with the terminal
    // node, which is always the last node in the row and labelled `done`: position tells those two
    // apart, the way it already told entry and terminal apart when both were slate.
    return <circle className={className} cx={x} cy={y} r={half} />;
  }
  return <rect className={className} x={x - half} y={y - half} width={size} height={size} />;
}

/**
 * A head-and-shoulders glyph, bottom-centred at (x, y), marking a step that waits on a person.
 *
 * The diamond already says "gate" to anyone who knows the legend; this says it to everyone else,
 * which is the whole audience for a dashboard someone opens once a week.
 *
 * On the step the ticket is actually sitting on it takes the gate's own colour rather than the
 * annotation grey, because there the glyph is the answer to "who is this waiting for" and not a
 * footnote to the shape below it.
 *
 * No `aria-hidden`: the root `<svg>` already carries the accessible name, so nothing in here is
 * announced on its own, and biome reads the attribute on a `<g>` as hiding a focusable element.
 */
function personIcon(x: number, y: number, className?: string) {
  return (
    <g className={cx("flow-node-person", className)}>
      <circle cx={x} cy={y - 7.2} r={2.4} />
      <path d={`M ${x - 4.2} ${y} A 4.2 4.2 0 0 1 ${x + 4.2} ${y} Z`} />
    </g>
  );
}

/**
 * The four corner marks around a selected node.
 *
 * Always mounted for any node that can be selected, and shown by class rather than by rendering:
 * a reticle that unmounts on deselect has nothing left to animate, and the fade-and-grow on the
 * way out is half of what was asked for. Its frame is fixed at the emphasised size — the size a
 * node has whenever it is selected — so the marks never jump as the node behind them resizes.
 *
 * Two nested elements because both carry a transform: the group eases scale and opacity, the
 * corners inside it pulse. One element could only do the last one to be declared.
 */
function Reticle({
  node,
  x,
  y,
  on,
}: {
  node: ProcessGraphNode;
  x: number;
  y: number;
  on: boolean;
}) {
  const frame = selectBox(x, y, node.label, node.kind, nodeSize(node.kind, EMPHASIS));
  return (
    <g
      className={cx("flow-node-reticle", on && "flow-node-reticle--on")}
      // Explicit user-space origin, not `fill-box`: the pulsing corners move this group's
      // bounding box, and an origin derived from it would drift with the pulse.
      style={{
        transformOrigin: `${frame.x + frame.width / 2}px ${frame.y + frame.height / 2}px`,
      }}
    >
      {selectCornerTriangles(frame).map((triangle, index) => (
        <polygon
          key={trianglePoints(triangle)}
          className="flow-node-select"
          points={trianglePoints(triangle)}
          style={reticleDirection(index)}
        />
      ))}
    </g>
  );
}

const TONE_WORDS: Record<NodeTone, string> = {
  error: "failed",
  attention: "needs attention",
  rejected: "rejected",
};

/** An attempt badge in words: `2` is two attempts so far, `1/2` the first of two. */
function badgeWords(badge: string): string | null {
  const [attempt, total] = badge.split("/");
  if (total) return `attempt ${attempt} of ${total}`;
  if (!attempt) return null;
  return attempt === "1" ? "1 attempt" : `${attempt} attempts`;
}

/**
 * A node's accessible name: the step, then what the drawing says about it in colour, size and
 * position — "implement: failed, attempt 2 of 2, current step" — so none of it is colour alone.
 */
export function nodeName(
  label: string,
  state: { tone?: NodeTone; badge?: string; current?: boolean; past?: boolean; muted?: boolean },
): string {
  const facts = [
    state.tone ? TONE_WORDS[state.tone] : null,
    state.badge ? badgeWords(state.badge) : null,
    state.current ? "current step" : state.muted ? "not reached" : null,
  ].filter(Boolean);
  return facts.length ? `${label}: ${facts.join(", ")}` : label;
}

/**
 * The mark a toned node carries at its shoulder, so a failed, parked or rejected step reads as a
 * shape as well as a colour: an exclamation for the first two, a cross for a rejection.
 */
function toneMark(x: number, y: number, half: number, tone: NodeTone) {
  const mx = x + half * 0.8;
  const my = y - half * 0.8;
  return (
    <g className={cx("flow-node-mark", `flow-node-mark--${tone}`)}>
      <circle cx={mx} cy={my} r={5.5} />
      {tone === "rejected" ? (
        <path
          className="flow-node-mark-cross"
          d={`M ${mx - 2} ${my - 2} L ${mx + 2} ${my + 2} M ${mx + 2} ${my - 2} L ${mx - 2} ${my + 2}`}
        />
      ) : (
        <>
          <rect
            className="flow-node-mark-glyph"
            x={mx - 0.75}
            y={my - 3.2}
            width={1.5}
            height={3.8}
          />
          <circle className="flow-node-mark-glyph" cx={mx} cy={my + 2.4} r={0.9} />
        </>
      )}
    </g>
  );
}

/**
 * Generic ranked graph. Process configs are one client; anything that can emit
 * `ProcessGraph` can reuse the same SVG.
 */
export function FlowGraph({
  graph,
  selectedId,
  onSelect,
  badges,
  currentId,
  currentBlinks = true,
  currentRunning = false,
  visitedIds,
  nodeTones,
}: {
  graph: ProcessGraph;
  selectedId?: string | null;
  onSelect?: (id: string | null) => void;
  /**
   * The line under a node's label. Strings, not counts: the client decides what a node's badge
   * says, and the ticket page says two different things with it — how many attempts a step has
   * had, and which of them the reader is currently in.
   */
  badges?: Record<string, string>;
  currentId?: string | null;
  /**
   * Whether the current node beats. The blink says "this is live, or it wants you", and a finished
   * ticket sitting on its terminal node is neither — so the ticket page turns it off for a done
   * ticket and the node simply holds its place.
   */
  currentBlinks?: boolean;
  /** Whether the current step's run is in progress. A running step beats for as long as it runs;
   *  any other current step beats three times and holds still. */
  currentRunning?: boolean;
  visitedIds?: ReadonlySet<string>;
  /**
   * What a node has to say beyond its kind and its position, keyed by node id — from
   * `ticketGraph.ts`'s `nodeTones`, which resolves it through the same table the status pills and
   * the rail dots use. Only nodes with something to say appear; the rest keep the graph's own
   * treatment. Absent entirely in schematic mode, where no ticket is being drawn.
   */
  nodeTones?: Record<string, NodeTone>;
}) {
  const markerId = useId().replace(/:/g, "");
  const byId = new Map(graph.nodes.map((node) => [node.id, node]));
  // The current step and the selected node draw larger, and an edge has to stop at the shape it
  // actually touches, so emphasis is resolved once here and read by both passes below.
  const emphasisOf = (id: string) => (id === currentId || id === selectedId ? EMPHASIS : 1);
  const halfOf = (node: ProcessGraphNode) => nodeSize(node.kind, emphasisOf(node.id)) / 2;
  const maxCol = graph.nodes.reduce((max, node) => Math.max(max, node.column), 0);
  const width = PAD_X * 2 + maxCol * COL_W;
  // The bottom allowance covers the tallest label on the stage, so a wrapped one is not cropped.
  const labelRows = graph.nodes.reduce(
    (max, node) => Math.max(max, wrapLabel(node.label).length),
    1,
  );
  // A long reject arc climbs above the node rows, and `.flow-graph-scroll` crops what leaves the
  // box. The viewBox grows upward to meet the tallest one rather than letting it be cut off;
  // a graph whose arcs stay inside `PAD_Y` is unaffected.
  const viewTop = Math.min(0, arcCeiling(graph, byId) - ARC_MARGIN);
  const height = PAD_Y * 2 + 28 + (labelRows - 1) * LABEL_LINE_H - viewTop;
  const selectable = Boolean(onSelect);

  return (
    <div className="flow-graph-scroll">
      {onSelect && (
        <button
          type="button"
          className="flow-graph-backdrop"
          aria-label="Clear node selection"
          onClick={() => onSelect(null)}
        />
      )}
      <svg
        className={cx("flow-graph", onSelect && "flow-graph--passthrough")}
        width={width}
        height={height}
        viewBox={`0 ${viewTop} ${width} ${height}`}
        style={{ "--flow-emphasis": EMPHASIS } as CSSProperties}
        // A group of buttons to pick a step with, or one picture of the process when there is
        // nothing to pick; never a listbox, which would promise arrow keys it does not have.
        role={selectable ? "group" : "img"}
        aria-label={
          selectable ? "Process steps" : graph.nodes.map((node) => node.label).join(", then ")
        }
      >
        <defs>
          <marker id={markerId} markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
            <path d="M0,0 L6,3 L0,6 Z" className="flow-edge-marker" />
          </marker>
        </defs>
        {graph.edges.map((edge) => {
          const fromNode = byId.get(edge.from);
          const toNode = byId.get(edge.to);
          if (!fromNode || !toNode) return null;
          return (
            <path
              key={`${edge.kind}:${edge.from}:${edge.to}`}
              className={edge.kind === "reject" ? "flow-edge flow-edge--reject" : "flow-edge"}
              d={edgePath(
                point(fromNode),
                point(toNode),
                edge.kind,
                halfOf(fromNode),
                halfOf(toNode),
              )}
              markerEnd={`url(#${markerId})`}
            />
          );
        })}
        {graph.nodes.map((node) => {
          const { x, y } = point(node);
          const selected = node.id === selectedId;
          const current = node.id === currentId;
          const schematic = visitedIds == null;
          const muted = Boolean(visitedIds && !visitedIds.has(node.id));
          const past = Boolean(visitedIds && !muted && !current);
          const tone = nodeTones?.[node.id];
          // A failure does not flash: its red and its mark say it. Only a live or waiting step
          // takes the beat, which the stylesheet stops after three unless the step is running.
          const alarming = tone === "error";
          // The end of the walk never beats. A blink says "this is live, or it wants you", and a
          // terminal node is neither — including in the tick between a ticket reaching that step
          // and the loop writing `done`, which is the one moment `currentBlinks` cannot speak for
          // because the ticket is still `running` when the page reads it.
          const beats = current && currentBlinks && node.kind !== "terminal";
          const outlined = schematic || muted;
          const canSelect = Boolean(onSelect) && !muted;
          const badge = badges?.[node.id] ?? "";
          const emphasised = emphasisOf(node.id) > 1;
          const size = nodeSize(node.kind, emphasisOf(node.id));
          const half = size / 2;
          const frame = selectBox(x, y, node.label, node.kind, size);
          const labelLines = wrapLabel(node.label);
          return (
            <g key={node.id} className={cx("flow-node-group", muted && "flow-node-group--muted")}>
              {canSelect && <Reticle node={node} x={x} y={y} on={selected} />}
              {node.kind === "human_gate" &&
                personIcon(
                  x,
                  y - half - PERSON_GAP,
                  cx(current && "flow-node-person--current", tone && `flow-node-person--${tone}`),
                )}
              {nodeShape(
                node.kind,
                x,
                y,
                nodeSize(node.kind),
                cx(
                  "flow-node",
                  outlined ? "flow-node--outline" : "flow-node--filled",
                  `flow-node--${node.kind}`,
                  emphasised && "flow-node--emphasised",
                  beats && !alarming && "flow-node--current",
                  beats && !alarming && currentRunning && "flow-node--running",
                  muted && "flow-node--muted",
                  past && !tone && "flow-node--past",
                  // Last, so a tone's fill wins over `--past`'s and over the kind colour beneath.
                  tone && `flow-node--tone-${tone}`,
                ),
              )}
              {tone && node.kind !== "human_gate" && toneMark(x, y, half, tone)}
              <text
                className="flow-small-label flow-node-label"
                x={x}
                y={y + LABEL_DY}
                textAnchor="middle"
              >
                {/* The full name stays reachable when a line was cut, so the diagram never
                    becomes the only place a step's name exists. */}
                {labelLines.join("") !== node.label && <title>{node.label}</title>}
                {labelLines.map((line, index) => (
                  // The label so far: unique per line, and stable, since the lines are one
                  // string split the same way every render and never reorder.
                  <tspan
                    key={labelLines.slice(0, index + 1).join("")}
                    x={x}
                    dy={index === 0 ? 0 : LABEL_LINE_H}
                  >
                    {line}
                  </tspan>
                ))}
              </text>
              {badge && (
                <text
                  className="flow-node-badge"
                  x={x}
                  y={y + labelDrop(labelLines) + BADGE_DY}
                  textAnchor="middle"
                >
                  {badge}
                </text>
              )}
              {canSelect && (
                <foreignObject
                  className="flow-node-hitbox"
                  x={frame.x}
                  y={frame.y}
                  width={frame.width}
                  height={frame.height}
                >
                  <button
                    type="button"
                    className="flow-node-button"
                    aria-pressed={selected}
                    aria-label={nodeName(node.label, { tone, badge, current, past, muted })}
                    onClick={(event) => {
                      event.stopPropagation();
                      onSelect?.(node.id);
                    }}
                  />
                </foreignObject>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
