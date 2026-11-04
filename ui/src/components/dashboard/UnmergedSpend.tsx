import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import type { UnmergedSpend as Unmerged } from "../../api/types";
import { formatCost, plural } from "../../format";
import { DataTable, EmptyState, Panel } from "../ui";

const HEAD = [
  { key: "what", content: "What" },
  { key: "count", content: "Count", align: "end" },
  { key: "cost", content: "Cost", align: "end" },
] as const;

type Row = { key: string; what: ReactNode; count: string; cost: string };

/** Four rules, each a row, and the union once as the last row: an attempt that fits two rows
 *  is in both and counted once in the total. */
export function UnmergedSpend({
  unmerged,
  ticketsHref,
}: {
  unmerged: Unmerged;
  ticketsHref: string;
}) {
  const rows: Row[] = [
    {
      key: "rework",
      what: "Rework attempts, entered by a gate rejection or a redo",
      count: plural(unmerged.rework.count, "attempt"),
      cost: formatCost(unmerged.rework.cost_usd, unmerged.rework.cost_estimated),
    },
    {
      key: "exited",
      what: (
        <Link to={`${ticketsHref}&status=exited`} className="hover:underline">
          Exited tickets (all their runs)
        </Link>
      ),
      count: plural(unmerged.exited.count, "ticket"),
      cost: formatCost(unmerged.exited.cost_usd, unmerged.exited.cost_estimated),
    },
    {
      key: "failed",
      what: "Failed or timed-out attempts",
      count: plural(unmerged.failed.count, "attempt"),
      cost: formatCost(unmerged.failed.cost_usd, unmerged.failed.cost_estimated),
    },
    {
      key: "set_aside",
      what: "Attempts a redo set aside",
      count: plural(unmerged.set_aside.count, "attempt"),
      cost: formatCost(unmerged.set_aside.cost_usd, unmerged.set_aside.cost_estimated),
    },
    {
      key: "total",
      what: <span className="font-medium">Total (each run counted once)</span>,
      count: "",
      cost: formatCost(unmerged.total_usd, unmerged.cost_estimated),
    },
  ];
  return (
    <Panel
      title="Spend without a merged change"
      subtitle="Rework, exited tickets, failed attempts and set-aside attempts."
      note="Rework counts attempts a gate rejection or a redo sent back. Exited counts every run of each exited ticket. Set aside counts the attempts a redo threw away. A run that fits two rows counts once in the total. Runs from before rework was recorded show none."
    >
      <DataTable<Row, (typeof HEAD)[number]["key"]>
        label="Spend without a merged change"
        head={[...HEAD]}
        rows={rows}
        rowKey={(row) => row.key}
        emptyView={<EmptyState header="Nothing yet" headingLevel={3} />}
        cells={(row) => ({ what: row.what, count: row.count, cost: row.cost })}
      />
    </Panel>
  );
}
