import type { ReactNode } from "react";
import type { AgentDetail } from "../../api/types";
import type { ProcessConfigLike } from "../flow";
import { ErrorMessage, type Glyph, Icon, Pressable, Tooltip } from "../ui";
import { stepsByAgent, type ToolChip, toolChips, toolPath } from "./agentSteps";

/** Where a lane's agents come from: the synced version's pinned commit, or a preview's own list. */
export interface AgentsSource {
  list: AgentDetail[] | undefined;
  pending: boolean;
  error: unknown;
}

/** A version's agents query as a source. `isLoading`, not `isPending`: with no version to read the
 *  query is disabled, and a disabled query with no data stays pending for ever. */
export function agentsFromQuery(query: {
  data?: AgentDetail[];
  isLoading: boolean;
  error: unknown;
}): AgentsSource {
  return { list: query.data, pending: query.isLoading, error: query.error };
}

function facts(agent: AgentDetail): string {
  const turns = agent.max_turns === null ? "no turn limit" : `${agent.max_turns} turns`;
  return [agent.model, turns].filter(Boolean).join(" · ");
}

const listed = (items: string[]) => (items.length > 0 ? items.join(", ") : "none");

/** Everything an agent's manifest says, for the hover over its card: the card itself keeps to
 *  what a reader scans for. */
export function agentDetails(agent: AgentDetail, steps: string[]): string {
  const permissions = agent.permissions
    ? Object.entries(agent.permissions)
        .map(([scope, level]) => `${scope}: ${level}`)
        .join(", ")
    : "the default profile";
  return [
    agent.name,
    `Runs: ${steps.join(", ")}`,
    `Model: ${agent.model ?? "the runtime's default"}`,
    `Limits: ${facts({ ...agent, model: null })}`,
    `Skills: ${listed([...agent.skills_shared, ...agent.skills_local])}`,
    `MCP servers: ${listed([...agent.mcp_shared, ...agent.mcp_local])}`,
    `Secrets: ${listed(agent.secrets)}`,
    `Permissions: ${permissions}`,
  ].join("\n");
}

/** Who uses a skill or server, said under its name. */
function usedBy(chip: ToolChip): string {
  if (chip.scope === "local") return `local to ${chip.agents[0]}`;
  return `shared · ${chip.agents.length} ${chip.agents.length === 1 ? "agent" : "agents"}`;
}

const GRID = "grid gap-2 sm:grid-cols-2 lg:grid-cols-3";

/** One group under the graph — agents, skills, MCP servers — each behind a rule of its own. */
function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="space-y-3 border-b border-default p-4">
      <h3 className="text-heading-xxs text-subtle">{title}</h3>
      {children}
    </div>
  );
}

/** What the lane's nodes run, under the graph, then the skills and MCP servers those agents use,
 *  each a group of its own. A card picks its agent's first step on the graph, where `StepDetails`
 *  has the full key/values; hovering one shows its manifest in brief. */
export function AgentCards({
  config,
  agents,
  selectedAgent,
  onPick,
  onOpenTool,
}: {
  config: ProcessConfigLike;
  agents: AgentsSource;
  selectedAgent: string | null;
  onPick: (agent: string) => void;
  /** Opens a skill's or server's file, by its path under the config folder. Absent in a
   *  preview, where there is no editor to open: the cards are then plain. */
  onOpenTool?: (path: string) => void;
}) {
  const byAgent = stepsByAgent(config);
  if (byAgent.size === 0) return null;
  const list = agents.list ?? [];
  const chips = toolChips(list.filter((agent) => byAgent.has(agent.name) && !agent.warning));
  const skills = chips.filter((chip) => chip.kind === "skill");
  const servers = chips.filter((chip) => chip.kind === "mcp");

  return (
    <>
      <Group title="Agents">
        <ErrorMessage error={agents.error} />
        <ul className={GRID}>
          {[...byAgent].map(([name, steps]) => {
            const agent = list.find((candidate) => candidate.name === name);
            const card = (
              <Pressable
                isSelected={name === selectedAgent}
                onClick={() => onPick(name)}
                className="flex w-full items-start gap-2 p-3"
              >
                <Icon glyph="agent" className="mt-0.5 text-icon-subtle" />
                <span className="min-w-0">
                  <span className="block font-mono text-body">{name}</span>
                  <span className="block text-body-sm text-subtle">{`runs ${steps.join(", ")}`}</span>
                  {/* Text, not a Spinner: a button may hold only phrasing content. */}
                  {agents.pending ? (
                    <span className="block text-body-sm text-subtle">Loading…</span>
                  ) : agent?.warning ? (
                    <span className="block text-body-sm text-warning">{agent.warning}</span>
                  ) : agent ? (
                    <span className="block text-body-sm text-subtle">{facts(agent)}</span>
                  ) : agents.error ? null : (
                    <span className="block text-body-sm text-subtle">not in the synced config</span>
                  )}
                </span>
              </Pressable>
            );
            return (
              <li key={name} data-agent-card={name}>
                {agent && !agent.warning ? (
                  <Tooltip content={agentDetails(agent, steps)} describes>
                    {card}
                  </Tooltip>
                ) : (
                  card
                )}
              </li>
            );
          })}
        </ul>
      </Group>
      {/* Both groups always, once the agents are read: an empty one says there are none, where
          a missing one looked like a section the page forgot. */}
      {!agents.pending && (
        <>
          <Group title="Skills">
            {skills.length > 0 ? (
              <ToolCards chips={skills} glyph="file-markdown" onOpenTool={onOpenTool} />
            ) : (
              <p className="text-body-sm text-subtle">No agent in this process uses a skill.</p>
            )}
          </Group>
          <Group title="MCP servers">
            {servers.length > 0 ? (
              <ToolCards chips={servers} glyph="plug" onOpenTool={onOpenTool} />
            ) : (
              <p className="text-body-sm text-subtle">
                No agent in this process uses an MCP server.
              </p>
            )}
          </Group>
        </>
      )}
    </>
  );
}

function ToolCards({
  chips,
  glyph,
  onOpenTool,
}: {
  chips: ToolChip[];
  glyph: Glyph;
  onOpenTool?: (path: string) => void;
}) {
  return (
    <ul className={GRID}>
      {chips.map((chip) => {
        const body = (
          <>
            <Icon glyph={glyph} className="mt-0.5 text-icon-subtle" />
            <span className="min-w-0">
              <span className="block truncate font-mono text-body">{chip.name}</span>
              <span className="block text-body-sm text-subtle">{usedBy(chip)}</span>
            </span>
          </>
        );
        return (
          <li key={chip.key} data-tool-card={chip.key}>
            {onOpenTool ? (
              <Tooltip content="Open in the editor" describes>
                <Pressable
                  onClick={() => onOpenTool(toolPath(chip))}
                  className="flex w-full items-start gap-2 p-3"
                >
                  {body}
                </Pressable>
              </Tooltip>
            ) : (
              <div className="flex items-start gap-2 rounded-md border border-default p-3">
                {body}
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}
