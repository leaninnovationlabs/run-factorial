import type { DashboardWeek } from "../../api/types";
import { formatCost, formatDay, NO_VALUE } from "../../format";
import { DataTable, EmptyState, Panel } from "../ui";
import { TicketLink } from "./TicketLink";
import { utcDay } from "./time";

const HEAD = [
  { key: "week", content: "Week of" },
  { key: "done", content: "Done", align: "end" },
  { key: "exited", content: "Exited", align: "end" },
  { key: "cost", content: "Spend", align: "end" },
  { key: "closed", content: "Tickets" },
] as const;

/** UTC Monday weeks, newest first: closures, spend, and the tickets closed as links. A window
 *  clipped to the first ticket starts mid-week, so its oldest row says where its count starts;
 *  the newest row is the week `as_of` falls in, which is not over. */
export function Weekly({ weeks, since }: { weeks: DashboardWeek[]; since: string }) {
  const rows = [...weeks].reverse();
  const sinceDay = utcDay(since);
  const current = weeks[weeks.length - 1]?.week_start;
  const weekOf = (row: DashboardWeek) => {
    const day =
      row.week_start < sinceDay
        ? `${formatDay(row.week_start)} (from ${formatDay(sinceDay)})`
        : formatDay(row.week_start);
    return row.week_start === current ? `${day} (partial)` : day;
  };
  return (
    <Panel
      title="Weekly throughput"
      subtitle="Done and exited tickets and their spend, week by week."
      note="Weeks start on Monday (UTC). This week is partial. A ticket counts in the week of its done or exited event, so a ticket exited and later re-entered can count twice."
    >
      <DataTable<DashboardWeek, (typeof HEAD)[number]["key"]>
        label="Weekly throughput"
        head={[...HEAD]}
        rows={rows}
        rowKey={(row) => row.week_start}
        emptyView={<EmptyState header="No weeks yet" headingLevel={3} />}
        cells={(row) => ({
          week: weekOf(row),
          done: row.done,
          exited: row.exited,
          cost:
            Number(row.cost_usd) === 0 ? NO_VALUE : formatCost(row.cost_usd, row.cost_estimated),
          closed: (
            <span className="flex flex-wrap gap-x-2">
              {row.closed.map((ticket) => (
                <TicketLink
                  key={`${ticket.ticket_id}-${ticket.outcome}`}
                  id={ticket.ticket_id}
                  external_key={ticket.external_key}
                />
              ))}
            </span>
          ),
        })}
      />
    </Panel>
  );
}
