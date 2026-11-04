/**
 * Which of a process's labels the Ticket labels section shows, and in what order.
 *
 * Extracted from the section so the rule can be read and tested without a DOM, the way
 * `activateButton.ts` and `commitGate.ts` are. The rule itself is small and easy to get subtly
 * wrong: `exists` has three states, and the third must not be sorted into either of the other
 * two just because it is falsy.
 */

import type { LabelSpec } from "../../api/types";

/**
 * `missing` is the default because it is the only view that asks the reader for anything. The
 * rest of the set is a reference, and a reference nobody is looking at should not be the first
 * thing on screen.
 */
export type LabelFilter = "missing" | "present" | "all";

/** A label the App could not check is neither missing nor present, so it appears under `all`
 *  alone rather than being guessed into one of the other two. */
export function filterLabels(labels: readonly LabelSpec[], filter: LabelFilter): LabelSpec[] {
  if (filter === "all") {
    // Missing first: the reader came for those, and the order is what saves them a scroll
    // through a set that is mostly fine. Stable, so the server's own order survives within
    // each half.
    return [...labels].sort((a, b) => Number(a.exists !== false) - Number(b.exists !== false));
  }
  return labels.filter((label) => label.exists === (filter === "present"));
}

export function labelCounts(labels: readonly LabelSpec[]): Record<LabelFilter, number> {
  return {
    missing: labels.filter((label) => label.exists === false).length,
    present: labels.filter((label) => label.exists === true).length,
    all: labels.length,
  };
}
