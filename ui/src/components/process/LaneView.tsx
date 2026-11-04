import type { ReactNode } from "react";
import { type ProcessConfigLike, ProcessFlowGraph } from "../flow";
import { AgentCards, type AgentsSource } from "./AgentCards";
import { agentOfStep, stepsByAgent } from "./agentSteps";
import { StepDetails } from "./StepDetails";

/** One lane as the section draws it, live or previewed: the graph, what its nodes run, and the
 *  selected node's key/values. Selection is the caller's, so a lane change can clear it. */
export function LaneView({
  config,
  agents,
  selectedId,
  onSelect,
  caption,
  actions,
  footerStart,
  footerEnd,
  onOpenTool,
}: {
  config: ProcessConfigLike;
  agents: AgentsSource;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  caption?: ReactNode;
  actions?: ReactNode;
  footerStart?: ReactNode;
  footerEnd?: ReactNode;
  /** Opens a skill's or MCP server's file in the editor; absent in a preview. */
  onOpenTool?: (path: string) => void;
}) {
  const selectedAgent = agentOfStep(config, selectedId);

  function pick(agent: string) {
    onSelect(agent === selectedAgent ? null : (stepsByAgent(config).get(agent)?.[0] ?? null));
  }

  return (
    <>
      <div className="border-b border-default p-4">
        <ProcessFlowGraph
          config={config}
          selectedId={selectedId}
          onSelect={onSelect}
          caption={caption}
          actions={actions}
          footerStart={footerStart}
          footerEnd={footerEnd}
        />
      </div>
      <AgentCards
        config={config}
        agents={agents}
        selectedAgent={selectedAgent}
        onPick={pick}
        onOpenTool={onOpenTool}
      />
      {selectedId && <StepDetails agents={agents} config={config} nodeId={selectedId} />}
    </>
  );
}
