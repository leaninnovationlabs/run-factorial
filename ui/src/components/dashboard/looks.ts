/**
 * The looks `dashboard.css` declares, as the one list both the sections and `tokens.test.ts`
 * read. A step's series follows its position in the process config, so the same step keeps its
 * colour across tickets, windows and refetches; past the fourth step the colours stop and the
 * rest are muted. A rework look and `idle` share a colour with another look and are told apart
 * by the hatch in `dashboard.css`.
 */

export const SERIES = 4;

export const DASH_LOOKS = [
  "outcome-done",
  "outcome-open",
  "outcome-exited",
  "series-1",
  "series-2",
  "series-3",
  "series-4",
  "rework-1",
  "rework-2",
  "rework-3",
  "rework-4",
  "retry",
  "muted",
  "idle",
] as const;

export type DashLook = (typeof DASH_LOOKS)[number];

/** The look of a step's first attempt, by the step's 0-based position among the agent steps. */
export function seriesLook(position: number): DashLook {
  return position < SERIES ? (`series-${position + 1}` as DashLook) : "muted";
}

/** The look of a rework attempt at that step: the step's own colour, under the hatch. */
export function reworkLook(position: number): DashLook {
  return position < SERIES ? (`rework-${position + 1}` as DashLook) : "muted";
}
