import { Link } from "react-router-dom";
import type { TicketListItem } from "../../api/types";
import type { ForgeHost } from "../../forge";
import { figureSizers, formatCost, formatTokens, NO_VALUE } from "../../format";
import { CONDITION_LABELS, conditionOf, LABEL_VARIANTS } from "../../status";
import {
  Button,
  Code,
  DataTable,
  EmptyState,
  Metric,
  RelativeTime,
  Select,
  type SortOrder,
  StatusLozenge,
  Tag,
  useStableOrder,
} from "../ui";
import { ADMISSION, type TicketFilter } from "./admissionFilter";
import { IssueActions } from "./IssueActions";

/**
 * The filter selects on the ticket's own `status`, which is coarser than the lozenge each row
 * draws: `blocked` holds escalations, rejections and interrupts alike. Hence "Stopped" rather than
 * "Blocked" (`LABEL_VARIANTS.statusFilter`) — the filter has to name the set it actually selects,
 * or a row reading "Interrupted" under a filter reading "Blocked" looks like a mistake in one of
 * the two.
 *
 * Quarantine gets no option for the same reason, in the other direction: it is not a `status` at
 * all (decision 31) — a quarantined ticket keeps whatever status it had — so a filter here could
 * not select on it without lying about what it selects. The row's lozenge still says so.
 *
 * `admission` is the exception the rule allows: its rows are not tickets at all but the open
 * issues that are not tickets YET, which is why it sits in a group of its own at the end rather
 * than among the statuses. The table swaps its columns for it — see `AdmissionTable`.
 *
 * One select rather than a row of buttons: seven buttons with counts wrapped to three uneven
 * lines in a panel's header, and a phone gave them a line each. Each option carries its count.
 */
const FILTERS: { label: string; status?: TicketFilter }[] = [
  { label: "All" },
  { label: CONDITION_LABELS.running, status: "running" },
  { label: CONDITION_LABELS.waiting_gate, status: "waiting_gate" },
  { label: LABEL_VARIANTS.statusFilter.blocked, status: "blocked" },
  { label: CONDITION_LABELS.done, status: "done" },
  { label: CONDITION_LABELS.exited, status: "exited" },
  { label: "Admission", status: ADMISSION },
];

export function TicketFilters({
  value,
  onChange,
  counts,
}: {
  value: TicketFilter | undefined;
  onChange: (status: TicketFilter | undefined) => void;
  counts: Partial<Record<TicketFilter, number>> & { all: number };
}) {
  const option = (filter: (typeof FILTERS)[number]) => ({
    value: filter.status ?? "all",
    label: `${filter.label} (${filter.status ? (counts[filter.status] ?? 0) : counts.all})`,
  });
  return (
    <Select
      aria-label="Filter tickets by status"
      width="medium"
      value={value ?? "all"}
      onChange={(event) => {
        const next = event.target.value;
        onChange(next === "all" ? undefined : (next as TicketFilter));
      }}
      options={[
        ...FILTERS.filter((filter) => filter.status !== ADMISSION).map(option),
        {
          group: "Not tickets yet",
          options: FILTERS.filter((filter) => filter.status === ADMISSION).map(option),
        },
      ]}
    />
  );
}

export type TicketSort = "updated" | "runs" | "cost";

const HEAD = [
  { key: "issue", content: "Issue" },
  { key: "process", content: "Process" },
  { key: "step", content: "Step" },
  { key: "status", content: "Status" },
  { key: "owner", content: "Owner" },
  { key: "runs", content: "Runs", align: "end", isSortable: true },
  { key: "tokens", content: "Tokens" },
  { key: "cost", content: "Cost", align: "end", isSortable: true },
  { key: "updated", content: "Updated", isSortable: true },
] as const;

const COMPARE: Record<TicketSort, (a: TicketListItem, b: TicketListItem) => number> = {
  updated: (a, b) => a.updated_at.localeCompare(b.updated_at),
  runs: (a, b) => a.attempts - b.attempts,
  cost: (a, b) => Number(a.cost_usd) - Number(b.cost_usd),
};

export const TICKETS_PER_PAGE = 25;

export function TicketTable({
  tickets,
  repo,
  forge,
  isLoading,
  isFiltered = false,
  onClearFilter,
  sort = null,
  order = "desc",
  onSort,
  page = 1,
  onSetPage,
}: {
  tickets: TicketListItem[] | undefined;
  repo: string;
  forge?: ForgeHost;
  isLoading: boolean;
  isFiltered?: boolean;
  onClearFilter?: () => void;
  /** Null keeps the API's own order — newest first, stable under polling. */
  sort?: TicketSort | null;
  order?: SortOrder;
  onSort?: (sort: TicketSort, order: SortOrder) => void;
  page?: number;
  onSetPage?: (page: number) => void;
}) {
  const compare = sort
    ? order === "asc"
      ? COMPARE[sort]
      : (a: TicketListItem, b: TicketListItem) => COMPARE[sort](b, a)
    : null;
  const rows = useStableOrder(tickets, (ticket) => ticket.id, compare, `${sort}-${order}`);

  // One set of sizers for the whole table: `Metric` lays every figure of a unit into the same
  // cell, so a column of costs comes out one width and lines up on the digits a reader is
  // comparing down it, instead of each row hugging its own figure.
  const sizers = figureSizers((tickets ?? []).flatMap((ticket) => Object.values(spendOf(ticket))));

  return (
    <DataTable
      label="Tickets"
      head={[...HEAD]}
      rows={rows}
      isLoading={isLoading}
      rowKey={(ticket) => ticket.id}
      rowHref={(ticket) => `/tickets/${ticket.id}`}
      isHeadSticky
      // What a phone keeps: which ticket, where it stands, and what it has cost.
      stack={{ title: "issue", aside: "status", meta: ["step", "process", "cost", "updated"] }}
      rowsPerPage={TICKETS_PER_PAGE}
      page={page}
      onSetPage={onSetPage}
      sortKey={sort ?? undefined}
      sortOrder={order}
      onSort={onSort ? (key, next) => onSort(key as TicketSort, next) : undefined}
      isFiltered={isFiltered}
      emptyView={
        <EmptyState
          header="No tickets yet"
          description="Issues appear here once the project is active and an issue meets the process's entry rule. The Admission filter shows issues that are still waiting."
        />
      }
      noResultsView={
        <EmptyState
          header="No tickets match this filter"
          primaryAction={onClearFilter && <Button onClick={onClearFilter}>Clear filter</Button>}
        />
      }
      cells={(ticket) => {
        const spend = spendOf(ticket);
        return {
          issue: (
            <div className="min-w-0">
              <span className="inline-flex items-center gap-1">
                <Link to={`/tickets/${ticket.id}`} className="font-medium hover:underline">
                  #{ticket.external_key}
                </Link>
                {/* The issue is the ticket's real home; the orchestrator is a projection of it.
                    Its own controls, so `useRowLink` leaves these clicks alone. */}
                <IssueActions
                  ticketId={ticket.id}
                  externalKey={ticket.external_key}
                  repo={repo}
                  forge={forge}
                />
              </span>
              {/* A second line rather than a column of its own: a title is the widest thing on
                  the row and the only field with no useful maximum, so it wraps rather than
                  hiding its end behind a tooltip. */}
              {ticket.title && (
                <p className="line-clamp-2 max-w-md text-body-sm text-subtle">{ticket.title}</p>
              )}
            </div>
          ),
          // A tag, not an axis: with several processes per project, a step name is no longer
          // unique even within one project, so the reader needs to know which lane it is in.
          process: <Tag>{ticket.process_key}</Tag>,
          step: <Code>{ticket.current_step}</Code>,
          // No run counts passed on purpose: `admitted` and `retrying` are the board's lanes, and
          // a row reading "Admitted" under a filter reading "Running" would be the very mismatch
          // the filter was relabelled to avoid. The blocked split is what this row needs.
          status: (
            <StatusLozenge
              status={conditionOf({
                status: ticket.status,
                latestRunStatus: ticket.latest_run_status,
                parkReason: ticket.park_reason,
                quarantined: ticket.quarantined_at !== null,
              })}
            />
          ),
          owner: <span className="text-subtle">{ticket.owner_at_admission || NO_VALUE}</span>,
          runs: ticket.attempts,
          tokens: (
            <Metric label="Tokens in and out" labelHidden figureSizers={sizers}>
              {spend.tokens}
            </Metric>
          ),
          cost: (
            <Metric label="Cost" labelHidden figureSizers={sizers}>
              {spend.cost}
            </Metric>
          ),
          updated: <RelativeTime iso={ticket.updated_at} />,
        };
      }}
    />
  );
}

/**
 * What a row spends, formatted — the same two figures, formatted the same way, that the ticket
 * page's own header states for one ticket.
 *
 * A ticket that has spent nothing reads `—` rather than `0 in / 0 out` and `$0.00`: the em dash
 * is the formatters' own word for "nothing yet", it is what the owner column beside it already
 * writes, and a row of zeroes is noise in a column someone is scanning for the expensive ticket.
 * Decided from the figures rather than from `attempts`, because a ticket whose only attempt is a
 * gate has run without spending anything.
 */
function spendOf(ticket: TicketListItem): { tokens: string; cost: string } {
  const used = ticket.input_tokens + ticket.output_tokens;
  return {
    tokens: used === 0 ? NO_VALUE : formatTokens(ticket.input_tokens, ticket.output_tokens),
    cost:
      Number(ticket.cost_usd) === 0 ? NO_VALUE : formatCost(ticket.cost_usd, ticket.cost_estimated),
  };
}
