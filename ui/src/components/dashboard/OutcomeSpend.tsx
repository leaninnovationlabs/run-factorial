import type { OutcomeSpend as OutcomeRow } from "../../api/types";
import { formatCost, plural } from "../../format";
import { Panel, StackedBar } from "../ui";
import { OUTCOME_LABELS, outcomeLook } from "./outcome";

/** One bar, part to whole, by what each ticket became, in the served order: done, open, exited.
 *  The label row under the bar is the legend and carries every value, a zero included. */
export function OutcomeSpend({ rows }: { rows: OutcomeRow[] }) {
  return (
    <Panel
      title="Spend by outcome"
      subtitle="All spend, split by where each ticket ended up."
      note="Outcome is where the ticket stands today: done, exited or still open. A ticket that never ran counts as open at $0."
    >
      <div className="px-4 py-4">
        <StackedBar
          label="Spend by outcome"
          segments={rows.map((row) => ({
            key: row.outcome,
            look: outcomeLook(row.outcome),
            label: `${OUTCOME_LABELS[row.outcome]}, ${plural(row.tickets, "ticket")}`,
            value: Number(row.cost_usd),
            text: formatCost(row.cost_usd, row.cost_estimated),
          }))}
        />
      </div>
    </Panel>
  );
}
