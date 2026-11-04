/** Seconds from one served instant to another, never negative: a gap is drawn as a length, and
 *  two attempts of a ticket can overlap by a tick. Zero when either end is missing. */
export function secondsBetween(from: string | null, to: string | null): number {
  if (!from || !to) return 0;
  return Math.max(0, (Date.parse(to) - Date.parse(from)) / 1000);
}

/** The instant's UTC calendar date: the weeks are UTC weeks, and Monday 00:00 UTC is still
 *  Sunday west of Greenwich. */
export const utcDay = (iso: string) => new Date(iso).toISOString().slice(0, 10);
