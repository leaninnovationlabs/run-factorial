/** Timestamp formatting. Every API timestamp is `timestamptz` serialized as ISO-8601 UTC. */

const ABSOLUTE = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

export const NO_VALUE = "-";

export function formatAbsolute(iso: string | null | undefined): string {
  if (!iso) return NO_VALUE;
  return ABSOLUTE.format(new Date(iso));
}

const CLOCK = new Intl.DateTimeFormat(undefined, { timeStyle: "medium" });

/** A time of day to the second, for an answer the reader asked for moments ago. */
export function formatClock(iso: string | null | undefined): string {
  if (!iso) return NO_VALUE;
  return CLOCK.format(new Date(iso));
}

const DAY = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", timeZone: "UTC" });

/** `Sep 14` for a date with no time, such as a week's Monday. UTC, so the day cannot slip back
 *  to the Sunday west of Greenwich. */
export function formatDay(isoDate: string): string {
  return DAY.format(new Date(`${isoDate}T00:00:00Z`));
}

const TIME_OF_DAY = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

/** `14:32`, the reader's clock to the minute, for an "as of" beside a figure. */
export function formatTimeOfDay(iso: string): string {
  return TIME_OF_DAY.format(new Date(iso));
}

const UNITS: [limit: number, seconds: number, name: Intl.RelativeTimeFormatUnit][] = [
  [60, 1, "second"],
  [3600, 60, "minute"],
  [86400, 3600, "hour"],
  [2592000, 86400, "day"],
  [31536000, 2592000, "month"],
  [Number.POSITIVE_INFINITY, 31536000, "year"],
];

const RELATIVE = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

/** "3 minutes ago" — the dashboard polls, so a reader needs to know how stale a row is. */
export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return NO_VALUE;
  const elapsed = (Date.now() - new Date(iso).getTime()) / 1000;
  const magnitude = Math.abs(elapsed);
  for (const [limit, seconds, unit] of UNITS) {
    if (magnitude < limit) return RELATIVE.format(-Math.round(elapsed / seconds), unit);
  }
  return formatAbsolute(iso);
}

/** A span of time, already measured. The one place the h/m/s shape is decided. */
export function formatElapsed(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  if (whole < 60) return `${whole}s`;
  const minutes = Math.floor(whole / 60);
  if (minutes < 60) return `${minutes}m ${whole % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

/** A duration that can run to days, such as a gate wait or a lead time: `1d 4h`, and under a day
 *  `formatElapsed`'s shape. */
export function formatDuration(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  if (whole < 86400) return formatElapsed(whole);
  const hours = Math.floor(whole / 3600);
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

/** How long a run took, or how long it has been going. */
export function formatRunTime(startIso: string | null, endIso: string | null): string {
  if (!startIso) return NO_VALUE;
  const end = endIso ? new Date(endIso).getTime() : Date.now();
  return formatElapsed((end - new Date(startIso).getTime()) / 1000);
}

/**
 * Cost arrives as a decimal string so it keeps its exact scale; don't do arithmetic on it.
 *
 * Shown to the cent, because that is the unit anyone reasons about a bill in. A run that cost
 * something but less than a cent reads `<$0.01` rather than `$0.00` — rounding a real charge to a
 * flat zero would say the run was free, which is a different claim from "too small to itemise".
 */
export function formatCost(value: string | null, estimated = false): string {
  if (value === null) return NO_VALUE;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return value;
  const amount = Math.max(0, parsed);
  // A list-price estimate, not a bill (`StepRun.cost_source`); the tilde is the whole label.
  const mark = estimated ? "~" : "";
  if (amount > 0 && amount < 0.005) return `${mark}<$0.01`;
  return `${mark}$${amount.toFixed(2)}`;
}

/** Compact counts for a graph caption: 22104 → 22.1K, 1200000 → 1.2M. */
export function formatCompactCount(value: number): string {
  const count = Math.max(0, value);
  if (count >= 1_000_000) return `${trimCompact(count / 1_000_000)}M`;
  if (count >= 1000) return `${trimCompact(count / 1000)}K`;
  return String(count);
}

function trimCompact(value: number): string {
  const fixed = value.toFixed(1);
  return fixed.endsWith(".0") ? fixed.slice(0, -2) : fixed;
}

/** `41.9K in / 9.2K out` — abbreviated, because the exact digit count is never the question. */
export function formatTokens(input: number | null, output: number | null): string {
  if (input === null && output === null) return NO_VALUE;
  return `${formatCompactCount(input ?? 0)} in / ${formatCompactCount(output ?? 0)} out`;
}

/**
 * One piece of a formatted metric: a figure with the unit that measures it, or the text written
 * around it (a separator, a comparison sign, the em dash that stands for "nothing yet").
 */
export type MetricPiece =
  | { kind: "text"; text: string }
  | { kind: "metric"; value: string; unit: string; unitFirst: boolean };

/**
 * The units the formatters above emit, plus the factory board's own (`factory-board/model.ts`,
 * which abbreviates with a lowercase `k` and names tokens in full).
 *
 * `K`/`k`/`M` are magnitudes attached to a figure rather than units, so they stay part of the
 * number they scale, and `h`/`m`/`s` count only directly after a figure so that no word is carved
 * up for a letter inside it.
 *
 * `retry`/`retries` is here despite a retry measuring nothing, and it is the one entry that is a
 * judgement rather than a fact about the formatters: the board needs it SPLIT OFF and shortened
 * (`components/ui/Metric.tsx`'s `UNIT_LABELS` writes it `×`), because the word is four times the
 * width of the figure it follows and it sits on a control that has room for a `$`. A count's other nouns — `8 issues`,
 * `5 of 8` — stay out, having nowhere that needs them abbreviated.
 */
const METRIC = /(\$)\s*([\d.,]+)|([\d.,]+[KkM]?)\s*(h|m|s|in|out|tok|retries|retry)\b/g;

/**
 * Split a formatted metric into its figures, their units, and whatever was written between them.
 *
 * The inverse of the formatters above, and deliberately next to them: a new format is written
 * here beside the rule that has to read it back, so the two cannot drift. Any text this does not
 * recognise survives as itself — a metric a reader cannot recognise is worse than an unstyled one.
 */
export function metricPieces(formatted: string): MetricPiece[] {
  const pieces: MetricPiece[] = [];
  let cursor = 0;
  for (const match of formatted.matchAll(METRIC)) {
    const [whole, currency, afterCurrency, figure, unit] = match;
    if (match.index > cursor) {
      pieces.push({ kind: "text", text: formatted.slice(cursor, match.index) });
    }
    pieces.push(
      currency
        ? { kind: "metric", value: afterCurrency as string, unit: currency, unitFirst: true }
        : { kind: "metric", value: figure as string, unit: unit as string, unitFirst: false },
    );
    cursor = match.index + whole.length;
  }
  if (cursor < formatted.length) pieces.push({ kind: "text", text: formatted.slice(cursor) });
  return pieces;
}

/**
 * Every distinct figure a set of formatted metrics writes for each unit.
 *
 * Collected from the strings BEFORE anything renders, for a layout CSS cannot do on its own: the
 * two lines of a metric button are separate boxes in separate grid rows, and nothing in the
 * cascade makes one box the size of another. The caller gathers the set once and hands it to
 * `Metric`, which lays every figure of a unit into the same cell — all but the real one invisible
 * — so the box comes out the width of the widest and is the same on whichever line it appears.
 *
 * The whole set rather than the longest of it, because the browser has to be the one that
 * measures. `1.0M` and `900k` are both four characters and `1.0M` is visibly wider, an `M` being
 * wider than a digit where a `k` is narrower — so a character count picks the wrong winner and
 * the box it sizes ends up a pixel or two out. Handing over every candidate is what makes the
 * answer exact in whatever font the skin is using.
 *
 * Keyed by UNIT rather than by position, because position is not stable: `1h 30m` over `45m` has
 * an hour in one line and not the other, and sizing the first box of each against the other would
 * measure an `h` against an `m`.
 */
export function figureSizers(formatted: string[]): Record<string, string[]> {
  const sizers: Record<string, string[]> = {};
  for (const text of formatted) {
    for (const piece of metricPieces(text)) {
      if (piece.kind !== "metric") continue;
      const unit = sizerUnit(piece.unit);
      const seen = sizers[unit] ?? [];
      sizers[unit] = seen;
      if (!seen.includes(piece.value)) seen.push(piece.value);
    }
  }
  return sizers;
}

/** The unit a figure is sized under. `1 retry` over `12 retries` is one count, drawn with one
 *  `×`, so both lines have to share one box width. */
export function sizerUnit(unit: string): string {
  return unit === "retry" ? "retries" : unit;
}

/** `1 step`, `3 steps`: a count and its noun, the plural given where an `s` will not do. */
export function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** `abc1234` — the convention every layer uses for a commit (factorial/source.py `_origin`). */
export function shortSha(sha: string | null): string {
  return sha ? sha.slice(0, 7) : NO_VALUE;
}

export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function errorField(error: unknown, field: "hint" | "detail"): string | null {
  if (typeof error !== "object" || error === null || !(field in error)) return null;
  const value = (error as Record<typeof field, unknown>)[field];
  return typeof value === "string" && value !== "" ? value : null;
}

/** The next step an `ApiError` names beside its message, if any. Read by shape, so the
 *  components that show errors need not import the client. */
export function errorHint(error: unknown): string | null {
  return errorField(error, "hint");
}

/** The technical text an `ApiError` keeps behind its plain message, if any. */
export function errorDetails(error: unknown): string | null {
  return errorField(error, "detail");
}
