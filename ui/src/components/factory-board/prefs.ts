/**
 * What the board's settings drawer sets, and where it is kept between visits.
 *
 * The drawer holds the two things that are configured rather than asked: where each metric's ramp
 * turns, and how big a ticket is drawn. Both are decisions about the INSTRUMENTS, so re-making
 * them on every reload is the same as not having offered them — an operator who moved the cost
 * ramp to match their own budget meant it for longer than one page load.
 *
 * Deliberately NOT here: the focus, the pinned metric and the lane sort. Those are what is applied
 * right now, RESET is their way out, and a board that reopened three days later still bracketing
 * one project would be answering a question nobody had asked yet.
 *
 * The parsing is pure and total, because the stored value is untrusted: it survives a version of
 * the app that wrote a different shape, and a hand-edited entry. Anything unreadable falls back
 * to the default rather than throwing — a bad preference must not be able to blank the page.
 */

import { useCallback } from "react";
import { usePersistentState } from "../../storage";
import { DEFAULT_DENSITY, DENSITIES } from "./ModeBar";
import { DEFAULT_THRESHOLDS, METRICS, type Thresholds } from "./model";

export const PREFS_STORAGE_KEY = "sf-ui:board-prefs";

export interface BoardPrefs {
  thresholds: Thresholds;
  /** Index into `DENSITIES`. */
  density: number;
}

export const DEFAULT_PREFS: BoardPrefs = {
  thresholds: DEFAULT_THRESHOLDS,
  density: DEFAULT_DENSITY,
};

/**
 * A stored value, narrowed to something the board can draw.
 *
 * Field by field rather than all or nothing: a stored blob written before a metric existed should
 * still restore the three ramps it does carry, and a density index that has gone out of range —
 * the sizes were renumbered, someone typed one — should cost the reader their node size and not
 * their thresholds.
 */
export function parsePrefs(raw: string | null | undefined): BoardPrefs {
  if (!raw) return DEFAULT_PREFS;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return DEFAULT_PREFS;
  }
  if (typeof parsed !== "object" || parsed === null) return DEFAULT_PREFS;
  const stored = parsed as { thresholds?: unknown; density?: unknown };
  return {
    thresholds: parseThresholds(stored.thresholds),
    density: parseDensity(stored.density),
  };
}

function parseThresholds(value: unknown): Thresholds {
  if (typeof value !== "object" || value === null) return DEFAULT_THRESHOLDS;
  const stored = value as Record<string, { lower?: unknown; upper?: unknown } | undefined>;
  const thresholds = { ...DEFAULT_THRESHOLDS };
  for (const metric of METRICS) {
    const pair = stored[metric];
    if (typeof pair !== "object" || pair === null) continue;
    // A negative edge is not rejected the way an unparseable one is — it is simply not a
    // threshold any of these metrics can have, every one of them being a count or a cost.
    const lower = edge(pair.lower);
    const upper = edge(pair.upper);
    if (lower === null || upper === null) continue;
    thresholds[metric] = { lower, upper };
  }
  return thresholds;
}

function edge(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function parseDensity(value: unknown): number {
  if (typeof value !== "number" || !Number.isInteger(value)) return DEFAULT_DENSITY;
  return value >= 0 && value < DENSITIES.length ? value : DEFAULT_DENSITY;
}

export function serializePrefs(prefs: BoardPrefs): string {
  return JSON.stringify(prefs);
}

/** The preferences, restored at mount and written back on every change (`storage.ts`). */
export function useBoardPrefs(): {
  prefs: BoardPrefs;
  setThresholds: (next: Thresholds) => void;
  setDensity: (next: number) => void;
} {
  const [prefs, setPrefs] = usePersistentState(PREFS_STORAGE_KEY, parsePrefs, serializePrefs);
  return {
    prefs,
    setThresholds: useCallback(
      (thresholds: Thresholds) => setPrefs((current) => ({ ...current, thresholds })),
      [setPrefs],
    ),
    setDensity: useCallback(
      (density: number) => setPrefs((current) => ({ ...current, density })),
      [setPrefs],
    ),
  };
}
