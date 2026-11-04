import { type ReactNode, useMemo } from "react";
import type { NodeTone } from "../../status";
import { FlowGraph } from "./FlowGraph";
import { GridField } from "./GridField";
import { type ProcessConfigLike, processGraphFromConfig } from "./processGraph";
import { SmallLabel } from "./SmallLabel";

/**
 * A process snapshot drawn as a flow on a grid stage.
 * Pass any parsed config — the mapper does not assume a particular step list.
 */
export function ProcessFlowGraph({
  config,
  caption = null,
  actions,
  footerStart,
  footerEnd,
  selectedId,
  onSelect,
  badges,
  currentId,
  currentBlinks,
  currentRunning,
  visitedIds,
  nodeTones,
  compact = false,
}: {
  config: ProcessConfigLike;
  /**
   * The stage's top-left slot. Any node, so a caller can put a figure, a pill, or several things
   * there; `null` leaves the slot empty without removing it. It is absolutely positioned, so what
   * goes in — or does not — never moves the graph.
   */
  caption?: ReactNode;
  /** Overlay opposite the caption (a picker, spend, …). */
  actions?: ReactNode;
  /**
   * The stage's two bottom corners, absolutely positioned like the caption and the actions, so
   * what goes in them never moves the graph.
   *
   * `footerStart` reads as a caption and is wrapped in one; `footerEnd` holds a control and is
   * not, because the small tracked uppercase type a caption takes is wrong on a button.
   */
  footerStart?: ReactNode;
  footerEnd?: ReactNode;
  selectedId?: string | null;
  onSelect?: (id: string | null) => void;
  badges?: Record<string, string>;
  currentId?: string | null;
  currentBlinks?: boolean;
  currentRunning?: boolean;
  visitedIds?: ReadonlySet<string>;
  nodeTones?: Record<string, NodeTone>;
  compact?: boolean;
}) {
  const graph = useMemo(() => processGraphFromConfig(config), [config]);

  return (
    <GridField className={compact ? "flow-process flow-process--compact" : "flow-process"}>
      {caption != null && <SmallLabel className="flow-process__caption">{caption}</SmallLabel>}
      {actions && <div className="flow-process__actions">{actions}</div>}
      {footerStart != null && (
        <SmallLabel className="flow-process__footer-start">{footerStart}</SmallLabel>
      )}
      {footerEnd && <div className="flow-process__footer-end">{footerEnd}</div>}
      <FlowGraph
        graph={graph}
        selectedId={selectedId}
        onSelect={onSelect}
        badges={badges}
        currentId={currentId}
        currentBlinks={currentBlinks}
        currentRunning={currentRunning}
        visitedIds={visitedIds}
        nodeTones={nodeTones}
      />
    </GridField>
  );
}
