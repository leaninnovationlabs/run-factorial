import { useState } from "react";
import type { ProjectDashboard, StepSegment, TicketStepCosts } from "../../api/types";
import { formatCompactCount, formatCost, formatDuration, NO_VALUE } from "../../format";
import { runShown } from "../../status";
import {
  BarList,
  type BarRow,
  Button,
  Code,
  DataTable,
  Drawer,
  EmptyState,
  Legend,
  Panel,
  SegmentedControl,
  StatusLozenge,
} from "../ui";
import { type DashLook, reworkLook, seriesLook } from "./looks";
import { axisCost, ticketAxis } from "./TicketCosts";
import { TicketLabel, TicketLink } from "./TicketLink";
import { secondsBetween } from "./time";

/** `entered_by` in words: `label` in the table, `marker` after the name of a rework or retry
 *  segment. */
const ENTERED_BY = {
  next: { label: "Next", marker: "" },
  reject_rerun: { label: "Rework", marker: " rework" },
  admission: { label: "Admission", marker: "" },
  retry: { label: "Retry", marker: " retry" },
  restart: { label: "Restart", marker: "" },
  redo: { label: "Redo", marker: " redo" },
} as const;

const marker = (segment: StepSegment) =>
  segment.entered_by ? ENTERED_BY[segment.entered_by].marker : "";

const HEAD = [
  { key: "ticket", content: "Ticket" },
  { key: "step", content: "Step" },
  { key: "attempt", content: "Attempt", align: "end" },
  { key: "entered", content: "Entered by" },
  { key: "status", content: "Status" },
  { key: "model", content: "Model" },
  { key: "time", content: "Duration", align: "end" },
  { key: "tokens", content: "Tokens", align: "end" },
  { key: "cost", content: "Cost", align: "end" },
] as const;

type Row = StepSegment & { ticket_id: number; external_key: string };

type Measure = "cost" | "time";

type Part = { key: string; look: DashLook; value: number; tip: string };

/** A step's colour follows its place among the agent steps, so #67's build and #77's build are
 *  one colour across rows, windows and refetches. A rework attempt, a redo's included
 *  (decision 61), takes the step's `rework-N`, a retry is the danger role, and a step past the
 *  palette is muted. */
function lookOf(agentSteps: string[], segment: StepSegment): DashLook {
  if (segment.entered_by === "retry") return "retry";
  const position = agentSteps.indexOf(segment.step);
  if (position < 0) return "muted";
  return segment.entered_by === "reject_rerun" || segment.entered_by === "redo"
    ? reworkLook(position)
    : seriesLook(position);
}

function waiting(key: string, seconds: number, words: string): Part {
  const time = formatDuration(seconds);
  return { key, look: "idle", value: seconds, tip: `${words}: ${time}` };
}

/** In Duration, an attempt is its active seconds, followed by any time it sat parked, and preceded
 *  by the wait since the attempt before it (a gate's wait included, since gates are not drawn),
 *  so a bar runs from the first attempt's start to the last one's end. An attempt still running
 *  has no active seconds yet and draws only the wait before it. */
export function partsOf(ticket: TicketStepCosts, agentSteps: string[], measure: Measure): Part[] {
  return ticket.segments.flatMap((segment, index) => {
    const key = `${segment.step}-${segment.attempt}`;
    const name = `${segment.step} attempt ${segment.attempt}${marker(segment)}`;
    const look = lookOf(agentSteps, segment);
    if (measure === "cost") {
      const cost = formatCost(segment.cost_usd, segment.cost_estimated);
      const value = Number(segment.cost_usd);
      return [{ key, look, value, tip: `${name}: ${cost}` }];
    }
    const previous = ticket.segments[index - 1];
    const gap = previous ? secondsBetween(previous.ended_at, segment.started_at) : 0;
    const wait = gap > 0 ? [waiting(`gap-${key}`, gap, "waiting")] : [];
    if (segment.seconds === null) return wait;
    const time = formatDuration(segment.seconds);
    return [
      ...wait,
      { key, look, value: segment.seconds, tip: `${name}: ${time}` },
      ...(segment.parked_seconds > 0
        ? [waiting(`parked-${key}`, segment.parked_seconds, `${name} paused`)]
        : []),
    ];
  });
}

/**
 * Per-attempt segments for the costliest tickets, by cost on the axis of Costliest tickets so
 * the rows compare, or by duration on an axis of their own. A segment's name and figure are in
 * its tooltip and in the All attempts drawer, the table a phone reads, since one line of them per
 * bar made the chart too busy to read. Only agent attempts are drawn or listed: gates cost
 * nothing and their waits live in Step and gate durations, or in Duration as the wait before the
 * next attempt.
 */
export function StepCosts({ data, stepOrder }: { data: ProjectDashboard; stepOrder: string[] }) {
  const [measure, setMeasure] = useState<Measure>("cost");
  const [attemptsOpen, setAttemptsOpen] = useState(false);
  if (data.step_costs.length === 0) return null;
  const tickets = agentAttempts(data.step_costs);
  // Process order first; then steps the config no longer names, in run order.
  const agentSteps = [
    ...new Set([
      ...stepOrder,
      ...data.steps.filter((step) => step.kind === "agent").map((step) => step.step),
      ...tickets.flatMap((ticket) => ticket.segments.map((segment) => segment.step)),
    ]),
  ];
  const legend = new Map<DashLook, string>();
  for (const segment of tickets.flatMap((ticket) => ticket.segments)) {
    const look = lookOf(agentSteps, segment);
    legend.set(
      look,
      look === "muted"
        ? "Other steps"
        : look === "retry"
          ? ENTERED_BY.retry.label
          : `${segment.step}${marker(segment)}`,
    );
  }
  const rows: BarRow[] = tickets.map((ticket) => {
    const parts = partsOf(ticket, agentSteps, measure);
    const total = parts.reduce((sum, part) => sum + part.value, 0);
    if (parts.some((part) => part.look === "idle")) legend.set("idle", "Waiting");
    return {
      key: String(ticket.ticket_id),
      label: (
        <TicketLabel
          id={ticket.ticket_id}
          external_key={ticket.external_key}
          title={ticket.title}
        />
      ),
      value: total,
      text:
        measure === "cost"
          ? formatCost(ticket.cost_usd, ticket.cost_estimated)
          : formatDuration(total),
      segments: parts,
    };
  });
  return (
    <Panel
      title="Spend by step"
      subtitle="Each attempt on the five costliest tickets, in the order it ran."
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <SegmentedControl
            label="Measure"
            value={measure}
            onChange={setMeasure}
            options={[
              { value: "cost", label: "Cost" },
              { value: "time", label: "Duration" },
            ]}
          />
          <Button appearance="subtle" onClick={() => setAttemptsOpen(true)}>
            All attempts
          </Button>
        </div>
      }
      note="Agent attempts only. A rework attempt is one a gate rejection or a redo sent back. A retry is a run started again automatically after a failure. Each step keeps the same color in every chart. In Duration, paused time is drawn after the attempt it interrupted."
    >
      <div className="space-y-3 px-4 py-4">
        <Legend
          label="Step"
          items={[...legend].map(([look, label]) => ({ key: look, look, label }))}
        />
        {measure === "cost" ? (
          <BarList
            label="Spend by step"
            rows={rows}
            max={ticketAxis(data)}
            format={axisCost}
            labelWidth="narrow"
          />
        ) : (
          <BarList
            label="Duration by step"
            rows={rows}
            format={formatDuration}
            labelWidth="narrow"
          />
        )}
      </div>
      {attemptsOpen && (
        <Drawer
          title="All attempts"
          subtitle="Each agent attempt on these tickets, in the order it ran."
          onClose={() => setAttemptsOpen(false)}
        >
          <AttemptTable costs={data.step_costs} />
        </Drawer>
      )}
    </Panel>
  );
}

/** Agent attempts only, as Spend by step draws them: gates cost nothing and wait elsewhere. */
function agentAttempts(costs: TicketStepCosts[]): TicketStepCosts[] {
  return costs.map((ticket) => ({
    ...ticket,
    segments: ticket.segments.filter((segment) => segment.kind === "agent"),
  }));
}

/** Spend by step's figures as a table: every segment named with its model, duration, tokens and
 *  cost, the text a phone reads where the bars have no tooltip. */
function AttemptTable({ costs }: { costs: TicketStepCosts[] }) {
  const table: Row[] = agentAttempts(costs).flatMap((ticket) =>
    ticket.segments.map((segment) => ({
      ...segment,
      ticket_id: ticket.ticket_id,
      external_key: ticket.external_key,
    })),
  );
  return (
    <DataTable<Row, (typeof HEAD)[number]["key"]>
      label="Cost by attempt"
      head={[...HEAD]}
      rows={table}
      rowKey={(row) => `${row.ticket_id}-${row.step}-${row.attempt}`}
      emptyView={<EmptyState header="No agent attempts" headingLevel={3} />}
      cells={(row) => ({
        ticket: <TicketLink id={row.ticket_id} external_key={row.external_key} />,
        step: <Code>{row.step}</Code>,
        attempt: row.attempt,
        entered: row.entered_by ? ENTERED_BY[row.entered_by].label : NO_VALUE,
        status: <StatusLozenge status={runShown(row)} />,
        model: row.model ? <Code className="whitespace-nowrap">{row.model}</Code> : NO_VALUE,
        time: (
          <span className="whitespace-nowrap">
            {row.seconds === null ? NO_VALUE : formatDuration(row.seconds)}
          </span>
        ),
        tokens: formatCompactCount(row.tokens),
        cost: formatCost(row.cost_usd, row.cost_estimated),
      })}
    />
  );
}
