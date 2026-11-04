/**
 * What the node selected on the process-config graph is configured to do: read from the snapshot
 * the graph draws, and for an agent step from the agent list the caller hands in — the synced
 * version's, or a preview's.
 *
 * The snapshot is `dict[str, Any]` on the wire, so every field is read defensively and a missing
 * one drops its row rather than printing an empty value.
 */

import { Fragment, type ReactNode } from "react";
import { Link } from "react-router-dom";
import type { AgentDetail } from "../../api/types";
import { conditionCalls, ENTRY_NODE_ID, type ProcessConfigLike } from "../flow";
import { ErrorMessage, SectionMessage, Spinner } from "../ui";
import type { AgentsSource } from "./AgentCards";

type Json = Record<string, unknown>;

interface Row {
  term: string;
  value: ReactNode;
}

function obj(value: unknown): Json | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Json)
    : null;
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function strings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    const named = typeof item === "string" ? item : text(obj(item)?.file);
    return named ? [named] : [];
  });
}

function calls(condition: unknown): string | null {
  return joined(conditionCalls(condition));
}

function joined(values: string[]): string | null {
  return values.length > 0 ? values.join(", ") : null;
}

function pr(value: unknown): string | null {
  const ref = obj(value);
  const head = text(ref?.head);
  const base = text(ref?.base);
  return head && base ? `${head} into ${base}` : null;
}

function ownOrDefault(
  step: Json,
  defaults: Json | null,
  key: "timeout" | "retry",
  format: (value: unknown) => string | null,
): string | null {
  const own = format(step[key]);
  if (own) return own;
  const inherited = format(defaults?.[key]);
  return inherited ? `${inherited} (default)` : null;
}

function retry(value: unknown): string | null {
  const block = obj(value);
  if (!block) return null;
  const attempts = typeof block.maxAttempts === "number" ? `${block.maxAttempts} attempts` : null;
  const backoff = text(block.backoff);
  return [attempts, backoff && `${backoff} backoff`].filter(Boolean).join(", ") || null;
}

function stepRows(step: Json, config: Json): Row[] {
  const defaults = obj(config.defaults);
  const timeout = ownOrDefault(step, defaults, "timeout", text);
  const retries = ownOrDefault(step, defaults, "retry", retry);
  const next = { term: "next", value: text(step.next) };

  switch (step.kind) {
    case "agent": {
      const workspace = obj(step.workspace);
      const slug = text(config.runtime);
      return [
        { term: "agent", value: text(step.agent) },
        {
          term: "runtime",
          value: slug ? (
            <Link to={`/runtimes/${slug}`} className="hover:underline">
              {slug}
            </Link>
          ) : (
            "none"
          ),
        },
        {
          term: "workspace",
          value:
            text(workspace?.branch) && `${text(workspace?.branch)} from ${text(workspace?.base)}`,
        },
        { term: "inputs", value: joined(strings(step.inputs)) },
        { term: "preconditions", value: calls(step.preconditions) },
        { term: "postconditions", value: calls(step.postconditions) },
        { term: "timeout", value: timeout },
        { term: "retry", value: retries },
        next,
      ];
    }
    case "human_gate": {
      const gate = obj(step.gate);
      const onApprove = obj(gate?.onApprove);
      const rerun = text(obj(gate?.onReject)?.rerunStep);
      return [
        { term: "pull request", value: pr(gate?.pr) },
        { term: "approvers", value: joined(strings(gate?.approvers)) },
        {
          term: "approver permission",
          value: text(gate?.approverPermission) && `${text(gate?.approverPermission)} or higher`,
        },
        { term: "on approve", value: onApprove?.merge ? "merge the pull request" : "no merge" },
        { term: "on reject", value: rerun && `rerun ${rerun}` },
        next,
      ];
    }
    case "check":
      return [
        { term: "conditions", value: calls(step.conditions) },
        { term: "pull request", value: pr(step.pr) },
        { term: "timeout", value: timeout },
        { term: "retry", value: retries },
        next,
      ];
    case "action":
      return [
        { term: "action", value: text(step.action) },
        { term: "retry", value: retries },
        next,
      ];
    default:
      return [next];
  }
}

function scoped(local: string[], shared: string[]): string | null {
  return joined([...local.map((name) => `${name} (local)`), ...shared.map((n) => `${n} (shared)`)]);
}

function agentRows(agent: AgentDetail): Row[] {
  const turns = agent.max_turns === null ? "no turn limit" : `${agent.max_turns} turns`;
  return [
    { term: "model", value: agent.model },
    { term: "limits", value: turns },
    { term: "skills", value: scoped(agent.skills_local, agent.skills_shared) ?? "none" },
    { term: "MCP servers", value: scoped(agent.mcp_local, agent.mcp_shared) },
    { term: "secrets", value: joined(agent.secrets) },
    {
      term: "permissions",
      value:
        agent.permissions === null
          ? "default profile"
          : joined(Object.entries(agent.permissions).map(([scope, level]) => `${scope}: ${level}`)),
    },
  ];
}

function Rows({ rows }: { rows: Row[] }) {
  const shown = rows.filter((row) => row.value !== null && row.value !== "");
  return (
    <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-6 gap-y-1.5 text-body-sm">
      {shown.map((row) => (
        <Fragment key={row.term}>
          <dt className="text-subtle">{row.term}</dt>
          <dd className="break-words text-default">{row.value}</dd>
        </Fragment>
      ))}
    </dl>
  );
}

function Heading({
  name,
  kind,
  description,
}: {
  name: string;
  kind: string;
  description?: string | null;
}) {
  return (
    <header className="mb-4 flex flex-wrap items-baseline gap-x-2 gap-y-1">
      <h3 className="font-mono text-heading-sm text-default">{name}</h3>
      <span className="text-body text-subtle">{kind}</span>
      {description && <span className="w-full text-body text-subtle">{description}</span>}
    </header>
  );
}

function AgentStatus({
  name,
  agent,
  agents,
}: {
  name: string;
  agent: AgentDetail | undefined;
  agents: AgentsSource;
}) {
  if (agents.error) return <ErrorMessage error={agents.error} className="mt-2" />;
  if (agents.pending) return <Spinner inline label={`Loading ${name}…`} />;
  if (!agent) {
    // A draft on the stage can name an agent the synced version never did.
    return (
      <p className="mt-2 text-body-sm text-subtle">
        {name} isn't in the synced config. Sync config to read it.
      </p>
    );
  }
  return agent.warning ? (
    <SectionMessage appearance="warning" className="mt-2">
      {agent.warning}
    </SectionMessage>
  ) : null;
}

export function StepDetails({
  agents,
  config,
  nodeId,
}: {
  agents: AgentsSource;
  config: ProcessConfigLike;
  nodeId: string;
}) {
  const raw = config as unknown as Json;
  const step = obj(config.steps.find((candidate) => candidate.name === nodeId));
  const agentName = step?.kind === "agent" ? text(step.agent) : null;

  let body: ReactNode;
  if (nodeId === ENTRY_NODE_ID) {
    const entry = obj(raw.entry);
    body = (
      <section>
        <Heading name="entry" kind="admission" />
        <Rows
          rows={[
            { term: "admits when", value: calls(entry?.when) },
            { term: "first step", value: text(entry?.step) },
          ]}
        />
      </section>
    );
  } else if (nodeId === config.terminal.step) {
    const actions = obj(obj(raw.terminal)?.actions);
    body = (
      <section>
        <Heading name={nodeId} kind="terminal" />
        <Rows rows={[{ term: "closes the issue", value: actions?.closeTicket ? "yes" : "no" }]} />
      </section>
    );
  } else if (step) {
    const rows = stepRows(step, raw);
    const agent = agentName
      ? agents.list?.find((candidate) => candidate.name === agentName)
      : undefined;
    if (agent && !agent.warning) {
      rows.splice(rows.findIndex((row) => row.term === "runtime") + 1, 0, ...agentRows(agent));
    }
    body = (
      <section>
        <Heading
          name={nodeId}
          kind={text(step.kind)?.replace("_", " ") ?? "step"}
          description={text(step.description)}
        />
        <Rows rows={rows} />
        {agentName && <AgentStatus name={agentName} agent={agent} agents={agents} />}
      </section>
    );
  } else {
    return null;
  }

  return <div className="border-b border-default p-4">{body}</div>;
}
