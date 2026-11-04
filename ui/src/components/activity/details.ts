import type { TimelineEntry } from "../../api/types";

/** Reading an event's `details`, which are `dict[str, Any]` on the wire: every value is checked
 *  for its type before it is used, and anything else reads as absent. */

export function str(value: unknown): string | null {
  if (typeof value === "string") return value.trim() || null;
  if (typeof value === "number") return String(value);
  return null;
}

/** A `due_at` the row can count down from — undefined for absent, or for anything unparseable. */
export function dueFrom(value: unknown): string | undefined {
  const due = str(value);
  return due && Number.isFinite(Date.parse(due)) ? due : undefined;
}

/** An issue or pull request number, as digits — null for anything that is not one. */
export function issueNumber(value: unknown): string | null {
  const raw = typeof value === "number" ? String(value) : typeof value === "string" ? value : null;
  return raw !== null && /^\d+$/.test(raw.trim()) ? raw.trim() : null;
}

/** The newest row, by id rather than position, that `match` accepts. */
export function newest(
  entries: readonly TimelineEntry[],
  match: (entry: TimelineEntry) => boolean,
): TimelineEntry | null {
  let found: TimelineEntry | null = null;
  for (const entry of entries) {
    if (match(entry) && (found === null || entry.id > found.id)) found = entry;
  }
  return found;
}
