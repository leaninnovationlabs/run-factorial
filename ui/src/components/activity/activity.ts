/**
 * The model behind the ticket page's activity region: what a reader is shown, and what is folded
 * away behind a click.
 *
 * Two jobs:
 *
 * 1. `segmentsFrom` — turn the flat event log into one block per ATTEMPT. A step that ran three
 *    times (a postcondition retry, then a rerun after a gate rejection) is three blocks, not one:
 *    collapsing them hides the reason the ticket needed a human. Every block still points at a
 *    single graph node, because the graph is drawn from the pinned config and knows nothing about
 *    attempts.
 *
 * 2. `factsFor` — decide, per event type, the ONE fact that earns space in a row, and what has to
 *    go in a modal instead. `agent_completed.summary` is whatever the adapter reported, with no
 *    length bound (`worker/agent_steps.py`), so rendering `details` verbatim lets a single event
 *    own the page.
 */

export { currentRows, type EventFacts, factsFor, isRoutine, type LongContent } from "./facts";
export {
  type EventLink,
  gatePullNumber,
  type LinkPulse,
  linkFor,
  linkPulse,
  waitingGatePr,
} from "./links";
export {
  type ActivityTotals,
  isLive,
  type Spawn,
  spawnsFrom,
  totalsFrom,
} from "./runs";
export {
  type ActivityOrder,
  type ActivitySegment,
  attemptKeyForNode,
  type Held,
  heldAt,
  segmentsFrom,
  sortSegments,
  statusDot,
} from "./segments";
export { snippet } from "./words";
