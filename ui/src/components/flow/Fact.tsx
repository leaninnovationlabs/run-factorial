/**
 * The stage's small-label type: a term and its value, laid out by `flow.css`'s `.flow-facts`.
 *
 * Shared rather than copied because two stages now caption themselves this way — the ticket's
 * graph states what is fixed about the run, and the configuration graph states which process and
 * commit it is drawing. Both sit in the caption slot, which is absolutely positioned, so however
 * many facts there are they cost the graph no height.
 *
 * A `<dl>` inside the caption's `<span>` would not be valid markup, and these are not really a
 * description list anyway.
 */

import type { ReactNode } from "react";

export function FlowFacts({ children }: { children: ReactNode }) {
  return <span className="flow-facts">{children}</span>;
}

export function Fact({ term, value }: { term: string; value: string }) {
  return (
    <span>
      {term}
      <span className="flow-facts__value">{value}</span>
    </span>
  );
}
