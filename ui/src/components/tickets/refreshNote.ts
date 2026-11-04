import type { RefreshResult } from "../../api/types";
import { formatClock } from "../../format";

/**
 * The words a refresh leaves at the end of the ticket's facts row — not under the button, where
 * they widened the actions until they wrapped below a long title. Kept to a few words because
 * they share that row with the facts. Each is about the click that produced it, so it stays true
 * while the page goes on polling underneath it. A refusal is not one of these: it is an error,
 * and the page draws it as one.
 */
export function refreshNote(state: { data?: RefreshResult }, slow = false): string | null {
  if (!state.data) return slow ? "refreshing…" : null;
  const asked = formatClock(state.data.requested_at);
  // The API stopped waiting first; the pass may still come, and the page's polling shows it.
  if (!state.data.landed) return `refresh sent at ${asked}`;
  // A pass that raised still releases the ticket, so it lands; it records the failure first. The
  // page's "Trouble updating this ticket" message says it keeps trying, so this need not.
  return (state.data.ticket.consecutive_failures ?? 0) > 0
    ? `refresh at ${asked} failed`
    : `updated at ${asked}`;
}
