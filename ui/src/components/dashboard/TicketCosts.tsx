import { Link } from "react-router-dom";
import type { ProjectDashboard } from "../../api/types";
import { formatCompactCount, formatCost, plural } from "../../format";
import { BarList, type BarRow, Legend, LinkButton, Panel, statusFacts } from "../ui";
import { OUTCOME_LABELS, OUTCOMES, outcomeLook, outcomeOf } from "./outcome";
import { TicketLabel } from "./TicketLink";

/** The costliest named ticket's spend: the axis of this list and of `StepCosts`, so their rows
 *  compare. The fold is not a bar on it, since the rest summed can outgrow every named ticket. */
export function ticketAxis(data: ProjectDashboard): number {
  return data.by_ticket.reduce((most, ticket) => Math.max(most, Number(ticket.cost_usd)), 0);
}

/** The axis labels of both cost lists. */
export const axisCost = (value: number) => formatCost(value.toFixed(2));

/**
 * The costliest tickets as bars on one axis, coloured by outcome so the figure above and this one
 * share a key. The rest and the tickets that never ran are a line of text under the bars; it and
 * the panel's action lead to the Tickets page, which is the full list.
 */
export function TicketCosts({
  data,
  ticketsHref,
}: {
  data: ProjectDashboard;
  ticketsHref: string;
}) {
  const { by_ticket: tickets, by_ticket_rest: rest, unrun_tickets: unrun } = data;
  const byCost = `${ticketsHref}&sort=cost-desc`;
  const rows: BarRow[] = tickets.map((ticket) => ({
    key: String(ticket.ticket_id),
    label: (
      <TicketLabel
        id={ticket.ticket_id}
        external_key={ticket.external_key}
        title={ticket.title}
        tip={[statusFacts(ticket.status).label, ticket.title].filter(Boolean).join(" · ")}
      />
    ),
    value: Number(ticket.cost_usd),
    text: formatCost(ticket.cost_usd, ticket.cost_estimated),
    look: outcomeLook(outcomeOf(ticket.status)),
    detail: `${plural(ticket.attempts, "run")} · ${formatCompactCount(ticket.tokens)} tokens`,
  }));
  return (
    <Panel
      title="Costliest tickets"
      subtitle="Spend per ticket, highest first."
      actions={
        <LinkButton to={byCost} appearance="subtle">
          All tickets
        </LinkButton>
      }
      note={`A ticket's cost counts every run on it: agent steps, gates and checks. The ${data.limits.by_ticket} costliest are listed, and the rest fold into one line. The All tickets link sorts by lifetime cost.`}
    >
      <div className="space-y-3 px-4 py-4">
        <Legend
          label="Outcome"
          items={OUTCOMES.map((outcome) => ({
            key: outcome,
            look: outcomeLook(outcome),
            label: OUTCOME_LABELS[outcome],
          }))}
        />
        <BarList
          label="Costliest tickets"
          rows={rows}
          max={ticketAxis(data)}
          format={axisCost}
          labelWidth="narrow"
        />
        {(rest || unrun > 0) && (
          <p className="text-body-sm text-subtle">
            {rest && (
              <>
                <Link to={byCost} className="hover:underline">
                  {plural(rest.tickets, "more ticket")}
                </Link>
                {`, ${formatCost(rest.cost_usd, rest.cost_estimated)} in all`}
              </>
            )}
            {rest && unrun > 0 && " · "}
            {unrun > 0 && (
              // Lifetime whatever the span, and not the remainder of the rows above under a span.
              <Link to={ticketsHref} className="hover:underline">
                {`${unrun} with no runs${data.span === "all" ? "" : ", all time"}`}
              </Link>
            )}
          </p>
        )}
      </div>
    </Panel>
  );
}
