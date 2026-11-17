/**
 * The line over the composer that says which project a message is about. Every message is sent
 * with the dashboard page the reader is asking about, and a project's page or a ticket's tells
 * the model the project; this says the same to the person, who otherwise cannot see it. Nothing
 * is drawn for a page in no project.
 */

import { useProject, useTicket } from "../../api/queries";
import { placeOf } from "../project/place";

/** The name of the project `page` is in: its own for a project's page, the ticket's for a
 *  ticket's. Undefined for any other page, and until the reads land. */
export function useProjectName(page: string | null): string | undefined {
  const place = page ? placeOf(page) : null;
  const ticket = useTicket(place?.kind === "ticket" ? place.ticketId : 0);
  const projectId =
    place?.kind === "project" ? place.place.projectId : (ticket.data?.project_id ?? 0);
  return useProject(projectId).data?.name;
}

export function AskingAbout({ page }: { page: string | null }) {
  const name = useProjectName(page);
  if (!name) return null;
  return (
    <p data-asking-about className="text-body-sm text-subtle">
      Asking about <span className="font-semibold text-default">{name}</span>
    </p>
  );
}
