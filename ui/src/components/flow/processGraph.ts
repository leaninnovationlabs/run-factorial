/**
 * Turn a parsed `process.yaml` snapshot into a ranked graph.
 *
 * The orchestrator stores camelCase aliases (`onReject.rerunStep`); older or hand-built
 * snapshots may still use snake_case. Both spellings are accepted so the same renderer can
 * draw any config the UI encounters.
 */

export type ProcessGraphKind = "entry" | "agent" | "human_gate" | "action" | "check" | "terminal";
export type ProcessGraphEdgeKind = "next" | "reject";

export interface ProcessGraphNode {
  id: string;
  label: string;
  kind: ProcessGraphKind;
  column: number;
}

export interface ProcessGraphEdge {
  from: string;
  to: string;
  kind: ProcessGraphEdgeKind;
}

export interface ProcessGraph {
  nodes: ProcessGraphNode[];
  edges: ProcessGraphEdge[];
}

type StepKind = "agent" | "human_gate" | "action" | "check";

interface RejectLike {
  rerunStep?: string;
  rerun_step?: string;
}

interface GateLike {
  onReject?: RejectLike | null;
  on_reject?: RejectLike | null;
  /** Read only by `nextNeeds`. */
  pr?: { head?: string; base?: string } | null;
}

export interface ProcessStepLike {
  name: string;
  kind: StepKind;
  next: string;
  description?: string | null;
  gate?: GateLike;
  /** Read only for its predicate calls — see `conditionCalls`. */
  preconditions?: unknown;
  /** Read for its predicate calls too, by `stepAwaitsReview`. */
  postconditions?: unknown;
  /** Read only by `hasPreview`. */
  preview?: unknown;
}

export interface ProcessConfigLike {
  name?: string;
  description?: string | null;
  entry: { step: string };
  steps: ProcessStepLike[];
  terminal: { step: string };
  /** The registry slug the process runs on. */
  runtime?: string | null;
}

export const ENTRY_NODE_ID = "__entry";

/**
 * Every predicate call in a condition tree, in source order.
 *
 * A predicate serializes to its own source string (`config/conditions.py` gives `PredicateCall` a
 * `model_serializer`), so the leaves already read the way config wrote them —
 * `ticket_has_assignee()`. The `all` / `any` / `not` structure above them is flattened rather
 * than rendered: the caller names what a step is waiting on in one line, and a reconstructed
 * boolean tree there would say less, not more.
 *
 * Both spellings are accepted for the same reason `rerunStepOf` accepts two — the orchestrator
 * dumps `by_alias=True`, but a hand-built snapshot may carry the attribute names instead.
 */
export function conditionCalls(condition: unknown): string[] {
  if (typeof condition === "string") return [condition];
  if (condition === null || typeof condition !== "object") return [];
  const node = condition as Record<string, unknown>;
  const branch = node.all ?? node.all_of ?? node.any ?? node.any_of;
  if (Array.isArray(branch)) return branch.flatMap(conditionCalls);
  const negated = node.not ?? node.not_;
  return negated === undefined ? [] : conditionCalls(negated);
}

export function rerunStepOf(step: ProcessStepLike): string | undefined {
  const reject = step.gate?.onReject ?? step.gate?.on_reject;
  if (!reject) return undefined;
  return reject.rerunStep ?? reject.rerun_step;
}

export function processGraphFromConfig(config: ProcessConfigLike): ProcessGraph {
  const byName = new Map(config.steps.map((step) => [step.name, step]));
  const terminal = config.terminal.step;

  const chain: string[] = [];
  const seen = new Set<string>();
  let current: string | undefined = config.entry.step;
  while (current && current !== terminal && !seen.has(current)) {
    seen.add(current);
    chain.push(current);
    current = byName.get(current)?.next;
  }

  const nodes: ProcessGraphNode[] = [
    { id: ENTRY_NODE_ID, label: "entry", kind: "entry", column: 0 },
  ];

  for (const [index, name] of chain.entries()) {
    const step = byName.get(name);
    nodes.push({
      id: name,
      label: name,
      kind: step?.kind ?? "agent",
      column: index + 1,
    });
  }

  nodes.push({
    id: terminal,
    label: terminal,
    kind: "terminal",
    column: chain.length + 1,
  });

  const edges: ProcessGraphEdge[] = [{ from: ENTRY_NODE_ID, to: config.entry.step, kind: "next" }];

  for (const name of chain) {
    const step = byName.get(name);
    if (!step) continue;
    edges.push({ from: name, to: step.next, kind: "next" });
    const rerun = rerunStepOf(step);
    if (rerun) edges.push({ from: name, to: rerun, kind: "reject" });
  }

  return { nodes, edges };
}

/**
 * Whether a snapshot has the three pointers the mapper walks.
 *
 * `process_version.content` is `dict[str, Any]` on the wire, and an old or hand-posted snapshot
 * can be missing any of it. The mapper would throw on `config.terminal.step` and take the whole
 * ticket page down with it, so a caller checks here first and renders nothing instead.
 */
export function isProcessConfigLike(value: unknown): value is ProcessConfigLike {
  if (typeof value !== "object" || value === null) return false;
  const config = value as Partial<ProcessConfigLike>;
  return (
    typeof config.entry?.step === "string" &&
    Array.isArray(config.steps) &&
    typeof config.terminal?.step === "string"
  );
}
