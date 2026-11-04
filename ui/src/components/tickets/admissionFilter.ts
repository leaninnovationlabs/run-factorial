import type { TicketStatus } from "../../api/types";

/**
 * The one filter value on the project's Tickets table that is not a ticket status.
 *
 * It selects the open issues that are NOT tickets yet — what the admission pass declined on its
 * last read — so it can never reach `GET /api/projects/{id}/tickets`, which takes statuses only.
 * Kept in its own module so both the control and the page can name it without the table and the
 * page importing each other.
 */
export const ADMISSION = "admission";

export type TicketFilter = TicketStatus | typeof ADMISSION;

/** The status the tickets query should ask for, which is nothing at all while the admission
 *  filter is up: its rows come from the pending route instead. */
export function ticketStatus(value: TicketFilter | undefined): TicketStatus | undefined {
  return value === undefined || value === ADMISSION ? undefined : value;
}
