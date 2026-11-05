/**
 * A project's tickets: the table, its status filter, sort and page, all kept in the URL so a view
 * can be shared by link and survives a reload. The Admission filter is the one whose rows are not
 * tickets — open issues the admission pass declines — and its read makes live GitHub calls, so it
 * polls only while this page is up.
 */

import { useBoard, usePendingIssues, useTickets } from "../api/queries";
import type { TicketStatus } from "../api/types";
import { ProjectPageState, useProjectRoute } from "../components/project";
import { AdmissionTable } from "../components/tickets/AdmissionTable";
import { ADMISSION, type TicketFilter, ticketStatus } from "../components/tickets/admissionFilter";
import { FleetRefresh } from "../components/tickets/FleetRefresh";
import { TicketFilters, type TicketSort, TicketTable } from "../components/tickets/TicketTable";
import {
  ErrorMessage,
  PageHeader,
  Panel,
  type SortOrder,
  staleSince,
  usePageTitle,
} from "../components/ui";
import { forgeHost } from "../forge";
import { useViewParams } from "../searchParams";

const TICKET_FILTERS = new Set<string>([
  "running",
  "waiting_gate",
  "blocked",
  "done",
  "exited",
  ADMISSION,
]);
const SORTS = new Set<string>(["updated", "runs", "cost"]);

function useTicketsView() {
  const [params, update] = useViewParams();
  const status = params.get("status");
  const [sortKey, sortOrder] = (params.get("sort") ?? "").split("-");
  const page = Number(params.get("page") ?? "1");
  return {
    status: status && TICKET_FILTERS.has(status) ? (status as TicketFilter) : undefined,
    sort: sortKey && SORTS.has(sortKey) ? (sortKey as TicketSort) : null,
    order: (sortOrder === "asc" ? "asc" : "desc") as SortOrder,
    page: Number.isInteger(page) && page > 0 ? page : 1,
    // A new filter or sort starts from the first page, or it could land past the end.
    setStatus: (value: TicketFilter | undefined) => update({ status: value ?? null, page: null }),
    setSort: (key: TicketSort, order: SortOrder) => update({ sort: `${key}-${order}`, page: null }),
    setPage: (value: number) => update({ page: value > 1 ? String(value) : null }),
  };
}

export function ProjectTicketsPage() {
  const { projectId, query, project } = useProjectRoute();
  const view = useTicketsView();
  const onAdmission = view.status === ADMISSION;
  const tickets = useTickets(projectId, ticketStatus(view.status), undefined, !onAdmission);
  // Polled whether or not its filter is up: the option carries the count, and a count that only
  // appears once you press the option cannot tell you there is anything to press it for.
  const pending = usePendingIssues(projectId);
  const board = useBoard(projectId);
  usePageTitle(["Tickets", project?.name]);

  if (!project) return <ProjectPageState projectId={projectId} query={query} />;
  const forge = forgeHost(project.ticket_source, project.settings, project.forge);

  // Counts for the filter come from the board, which is one grouped query rather than a request
  // per filter. It only covers open tickets, so "Done" is counted from the difference — less the
  // exited ones, which set `closed_at` too and would otherwise be absorbed into Done.
  const openByStatus = new Map<TicketStatus, number>();
  for (const cell of board.data?.cells ?? []) {
    openByStatus.set(cell.status, (openByStatus.get(cell.status) ?? 0) + cell.count);
  }
  const counts = {
    all: project.ticket_count,
    running: openByStatus.get("running") ?? 0,
    waiting_gate: openByStatus.get("waiting_gate") ?? 0,
    blocked: openByStatus.get("blocked") ?? 0,
    done: project.ticket_count - project.open_ticket_count - project.exited_ticket_count,
    exited: project.exited_ticket_count,
    [ADMISSION]: pending.data?.length ?? 0,
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title="Tickets"
        actions={<FleetRefresh projectId={project.id} />}
        staleSince={staleSince(query)}
      />
      <Panel
        // The heading follows the filter: under Admission the rows are open issues that are
        // not tickets yet, and a count of tickets above them would be counting something else.
        title={onAdmission ? "Awaiting admission" : "Tickets"}
        count={onAdmission ? counts[ADMISSION] : project.ticket_count}
        actions={<TicketFilters value={view.status} onChange={view.setStatus} counts={counts} />}
      >
        {onAdmission ? (
          pending.error && !pending.data ? (
            <ErrorMessage
              error={pending.error}
              title="Couldn't load the open issues"
              onRetry={() => pending.refetch()}
              className="m-4"
            />
          ) : (
            <AdmissionTable
              issues={pending.data}
              // Admission lists the ticket source's repo, which can differ from the code's
              // (`db.models.intake_repo`).
              repo={project.ticket_source?.external_ref || project.github_repo}
              forge={forge}
              isLoading={pending.isPending}
            />
          )
        ) : tickets.error && !tickets.data ? (
          <ErrorMessage
            error={tickets.error}
            title="Couldn't load the tickets"
            onRetry={() => tickets.refetch()}
            className="m-4"
          />
        ) : (
          <TicketTable
            tickets={tickets.data?.tickets}
            repo={project.github_repo}
            forge={forge}
            isLoading={tickets.isPending || tickets.isPlaceholderData}
            isFiltered={view.status !== undefined}
            onClearFilter={() => view.setStatus(undefined)}
            sort={view.sort}
            order={view.order}
            onSort={view.setSort}
            page={view.page}
            onSetPage={view.setPage}
          />
        )}
      </Panel>
    </div>
  );
}
