import type { DashboardLimits, OpenGate, StepTime } from "../../api/types";
import { formatDuration, NO_VALUE } from "../../format";
import { Code, type Column, DataTable, EmptyState, Panel, Tag } from "../ui";
import { NowChip } from "./NowChip";
import { TicketLink } from "./TicketLink";
import { secondsBetween } from "./time";

const KIND_LABELS: Record<string, string> = {
  agent: "agent",
  human_gate: "human gate",
  action: "action",
  check: "check",
};

type Key = "process" | "step" | "kind" | "runs" | "median" | "p90" | "longest";

/** A lookup, so a table and not a chart: each step's runs and how long they took, each gate's
 *  wait to a decision, ordered by first start; then the gates waiting now, longest first. The
 *  lane column appears only with more than one lane. */
export function StepsAndGates({
  steps,
  gates,
  openGates,
  asOf,
  limits,
}: {
  steps: StepTime[];
  gates: StepTime[];
  openGates: OpenGate[];
  asOf: string;
  limits: DashboardLimits;
}) {
  const rows = [...steps, ...gates];
  const lanes = new Set(rows.map((row) => row.process_key));
  const head: Column<Key>[] = [
    ...(lanes.size > 1 ? [{ key: "process" as const, content: "Process" }] : []),
    { key: "step", content: "Step" },
    { key: "kind", content: "Kind" },
    { key: "runs", content: "Runs", align: "end" },
    { key: "median", content: "Median", align: "end" },
    { key: "p90", content: "p90", align: "end" },
    { key: "longest", content: "Longest", align: "end" },
  ];
  return (
    <Panel
      title="Step and gate durations"
      subtitle="How often each step ran and how long it took. For gates, how long they waited for a decision."
      note={`Run times are measured in 30-second intervals, so a short run can show as 30s. Time a run sat paused for a person is left out. A gate's wait runs from opening to decision. The p90 shows once a step or gate has ${limits.p90_min_runs} runs. Waits an exit cut short are counted in Outliers on the Activity tab and in Where the time goes, but not here.`}
    >
      <DataTable<StepTime, Key>
        label="Step and gate durations"
        head={head}
        rows={rows}
        rowKey={(row) => `${row.process_key}-${row.step}`}
        emptyView={<EmptyState header="No step or gate has finished" headingLevel={3} />}
        cells={(row) => ({
          process: <Tag>{row.process_key}</Tag>,
          step: <Code>{row.step}</Code>,
          kind: KIND_LABELS[row.kind] ?? row.kind,
          runs: row.runs,
          median: formatDuration(row.median_seconds),
          p90: row.p90_seconds === null ? NO_VALUE : formatDuration(row.p90_seconds),
          longest: formatDuration(row.max_seconds),
        })}
      />
      <WaitingNow openGates={openGates} asOf={asOf} className="border-t border-default" />
    </Panel>
  );
}

/** The gates still open, longest first: a now figure, so the quiet span shows it too. */
export function WaitingNow({
  openGates,
  asOf,
  className,
}: {
  openGates: OpenGate[];
  asOf: string;
  className?: string;
}) {
  if (openGates.length === 0) return null;
  return (
    <Panel
      title={
        <span className="inline-flex items-center gap-2">
          Waiting now <NowChip />
        </span>
      }
      headingLevel={3}
      bare
      className={className}
    >
      <ul className="space-y-1 px-4 py-3 text-body">
        {openGates.map((gate) => (
          <li key={`${gate.ticket_id}-${gate.step}-${gate.attempt}`}>
            <TicketLink id={gate.ticket_id} external_key={gate.external_key} />{" "}
            <Code>{gate.step}</Code> attempt {gate.attempt}, waiting{" "}
            {formatDuration(secondsBetween(gate.waiting_since, asOf))}.
          </li>
        ))}
      </ul>
    </Panel>
  );
}
