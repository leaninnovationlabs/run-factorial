/**
 * The project the current page is in, for the shell: from the address on a project's own pages,
 * and from the ticket on a ticket's page, whose address names no project. The ticket's read is the
 * one its page already polls, so this costs no request of its own; until it lands the page is in
 * no project, and the navigation shows the organization's.
 */

import { useLocation } from "react-router-dom";
import { useTicket } from "../../api/queries";
import { type Place, type ProjectPlace, placeOf } from "./place";

export function useProjectPlace(): ProjectPlace | null {
  const { pathname } = useLocation();
  const place: Place = placeOf(pathname);
  const ticket = useTicket(place?.kind === "ticket" ? place.ticketId : 0);
  if (place?.kind === "project") return place.place;
  if (place?.kind === "ticket" && ticket.data) {
    return { projectId: ticket.data.project_id, area: "tickets", section: null };
  }
  return null;
}
