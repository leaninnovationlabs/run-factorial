import type { TicketDetail } from "../../api/types";
import { formatAbsolute, formatCost, formatElapsed, formatTokens, shortSha } from "../../format";
import type { TicketCondition } from "../../status";
import type { ActivityTotals } from "../activity";
import { Code, Icon, StatusLozenge, Tag } from "../ui";
import { SpendFigures } from "./SpendFigures";

/**
 * What is fixed about the ticket, under its title: its condition, the step it is on, who owned it
 * at admission, the lane and the commit its config was read from, and when it was admitted.
 */
export function TicketFacts({
  ticket,
  condition,
  process,
}: {
  ticket: TicketDetail;
  condition: TicketCondition | null;
  process: { process_key: string; source_commit_sha: string } | undefined;
}) {
  return (
    <>
      <StatusLozenge status={condition ?? ticket.status} />
      <span className="inline-flex items-center gap-1">
        <span className="text-subtlest">Step</span>
        <Code>{ticket.current_step}</Code>
      </span>
      <span className="inline-flex items-center gap-1">
        <Icon glyph="user" size="small" className="text-icon-subtle" />
        <span className="sr-only">Owner</span>
        {ticket.owner_at_admission || "Unassigned"}
      </span>
      {process && (
        <>
          <Tag>{process.process_key}</Tag>
          <span className="inline-flex items-center gap-1">
            <span className="text-subtlest">Config</span>
            <Code>{shortSha(process.source_commit_sha)}</Code>
          </span>
        </>
      )}
      <span>
        <span className="text-subtlest">Admitted</span> {formatAbsolute(ticket.admitted_at)}
      </span>
    </>
  );
}

/**
 * What the ticket has cost so far, in the Activity header rather than behind a click — "is this
 * one expensive, and where did the time go" is a question about the whole ticket, and the
 * per-attempt figures below only answer it if the reader adds them up themselves.
 */
export function TicketTotals({ totals }: { totals: ActivityTotals }) {
  if (totals.runs === 0) return null;
  return (
    <SpendFigures
      tokens={formatTokens(totals.inputTokens, totals.outputTokens)}
      cost={formatCost(totals.priced ? totals.costUsd : null, totals.estimated)}
      estimated={totals.estimated}
      time={formatElapsed(totals.seconds)}
    />
  );
}
