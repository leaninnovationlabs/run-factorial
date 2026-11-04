/**
 * The wire, mapped onto the board's own vocabulary.
 *
 * The one place that knows both shapes. Everything above `IssueNode` is written against the
 * board's names and never sees a snake_case key; everything below is the API's business. When the
 * endpoint gains a field, this is the file that changes.
 */

import type { OverviewResponse } from "../../api/types";
import type { IssueNode, ProjectBand } from "./model";

export function toBoard(response: OverviewResponse): {
  nodes: IssueNode[];
  projects: ProjectBand[];
} {
  const projects: ProjectBand[] = response.projects.map((p) => ({ id: p.id, name: p.name }));
  const known = new Set(projects.map((p) => p.id));
  const nodes: IssueNode[] = [];
  for (const t of response.tickets) {
    // A node with no band has nowhere to be drawn. `layout` would drop it anyway; dropping it
    // here is what keeps the total on the stats panel equal to what the board shows.
    if (!known.has(t.project_id)) continue;
    nodes.push({
      id: t.ticket_id,
      key: t.external_key,
      title: t.title,
      projectId: t.project_id,
      step: t.current_step,
      process: t.process_key,
      status: t.status,
      quarantined: t.quarantined_at !== null,
      latestRunStatus: t.latest_run_status,
      parkReason: t.park_reason,
      runs: t.runs,
      attempt: t.attempt,
      // A Decimal string on the wire so it keeps its scale. Left as one it would sort lexically
      // and sum by concatenation, and both failures read as bad data rather than a bad cast.
      costUsd: Number(t.cost_usd),
      costEstimated: t.cost_estimated,
      minutes: t.minutes,
      tokens: t.tokens,
    });
  }
  return { nodes, projects };
}
