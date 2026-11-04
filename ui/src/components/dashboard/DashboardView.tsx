/**
 * The project's dashboard: what it spent, what it delivered and where the time went over a
 * window of whole weeks, from one read (`useDashboard`, tech-stack decision 61), drawn by hand on
 * the chart design primitives (decision 62).
 *
 * The panels sit under Spend, Time and Activity sub-tabs, kept in the URL by the page. Every
 * sentence here names a rule and every number is served; the browser computes nothing but
 * a bar's width.
 */

import { useDashboard } from "../../api/queries";
import type { DashboardExtremes } from "../../api/types";
import { formatCompactCount, formatCost, formatDuration, plural } from "../../format";
import { projectTicketsHref } from "../project/place";
import {
  Code,
  cx,
  EmptyState,
  LinkButton,
  Panel,
  QueryBoundary,
  Skeleton,
  SkeletonRows,
  StaleNote,
  staleSince,
  Tab,
  TabList,
  TabPanel,
  Tabs,
} from "../ui";
import { Hero } from "./Hero";
import { NowChip } from "./NowChip";
import { OutcomeSpend } from "./OutcomeSpend";
import { StepCosts } from "./StepCosts";
import { StepsAndGates } from "./StepsAndGates";
import { useAgentStepOrder } from "./stepOrder";
import { TicketCosts } from "./TicketCosts";
import { TicketLink } from "./TicketLink";
import { TimeSplit } from "./TimeSplit";
import { secondsBetween } from "./time";
import { UnmergedSpend } from "./UnmergedSpend";
import { Weekly } from "./Weekly";

export type DashboardSection = "spend" | "time" | "activity";

export function DashboardView({
  projectId,
  section,
  onSectionChange,
}: {
  projectId: number;
  section: DashboardSection;
  onSectionChange: (section: DashboardSection) => void;
}) {
  // The payload still takes a window (MCP callers use it); the page shows all time only.
  const query = useDashboard(projectId, "all");
  const stepOrder = useAgentStepOrder(projectId);
  const ticketsHref = projectTicketsHref(projectId);
  return (
    <div className="space-y-4">
      <StaleNote updatedAt={staleSince(query)} />
      <QueryBoundary query={query} loading={<Loading />} errorTitle="Couldn't load the dashboard">
        {(data) => (
          <div className="space-y-4">
            <Hero data={data} />
            {data.lifetime.runs.total === 0 ? (
              <EmptyState
                header="Nothing has run yet"
                description="Figures appear here once the project's tickets start running."
                primaryAction={<LinkButton to={ticketsHref}>View tickets</LinkButton>}
              />
            ) : (
              <Tabs
                id={`project-${projectId}-dashboard`}
                selected={section}
                onChange={(value) => onSectionChange(value as DashboardSection)}
              >
                <TabList label="Dashboard sections" align="fill">
                  <Tab value="spend">Spend</Tab>
                  <Tab value="time">Time</Tab>
                  <Tab value="activity">Activity</Tab>
                </TabList>
                <TabPanel value="spend" className="space-y-6">
                  <OutcomeSpend rows={data.by_outcome} />
                  <div
                    className={cx(
                      "grid gap-6 [&>*]:min-w-0",
                      data.step_costs.length > 0 && "xl:grid-cols-2",
                    )}
                  >
                    <TicketCosts data={data} ticketsHref={ticketsHref} />
                    <StepCosts data={data} stepOrder={stepOrder} />
                  </div>
                  <UnmergedSpend unmerged={data.unmerged} ticketsHref={ticketsHref} />
                </TabPanel>
                <TabPanel value="time" className="space-y-6">
                  <TimeSplit split={data.time_split} />
                  <StepsAndGates
                    steps={data.steps}
                    gates={data.gates}
                    openGates={data.open_gates}
                    asOf={data.as_of}
                    limits={data.limits}
                  />
                </TabPanel>
                <TabPanel value="activity" className="space-y-6">
                  <Callouts extremes={data.extremes} asOf={data.as_of} />
                  <Weekly weeks={data.weeks} since={data.since} />
                </TabPanel>
              </Tabs>
            )}
          </div>
        )}
      </QueryBoundary>
    </div>
  );
}

function Loading() {
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-5">
        {["spend", "per-done", "done", "velocity", "lead"].map((key) => (
          <Skeleton key={key} height="5.5rem" />
        ))}
      </div>
      <Panel title="Spend by outcome">
        <SkeletonRows rows={2} columns={3} />
      </Panel>
    </div>
  );
}

/** Fixed rules rather than a reading: the costliest run, the step that ran most often on one
 *  ticket, the longest gate wait, the slowest ticket to done, and the longest wait still open
 *  (a Now figure, timed on the served clock). A rule that finds nothing writes nothing. */
function Callouts({ extremes, asOf }: { extremes: DashboardExtremes; asOf: string }) {
  const {
    largest_run: largest,
    most_attempts: most,
    longest_gate: gate,
    slowest_done: slowest,
    longest_open_wait: open,
  } = extremes;
  if (!largest && !most && !gate && !slowest && !open) return null;
  return (
    <Panel
      title="Outliers"
      subtitle="The runs and tickets that stand out."
      note="Each line uses a fixed rule. A rule that finds nothing shows no line."
    >
      <ul className="space-y-1 px-4 py-3 text-body">
        {largest && (
          <li>
            Largest single run:{" "}
            <TicketLink id={largest.ticket_id} external_key={largest.external_key} />{" "}
            <Code>{largest.step}</Code> attempt {largest.attempt},{" "}
            {formatCost(largest.cost_usd, largest.cost_estimated)}
            {largest.seconds !== null && ` in ${formatDuration(largest.seconds)}`},{" "}
            {formatCompactCount(largest.tokens)} tokens
            {largest.model && (
              <>
                {" "}
                on <Code>{largest.model}</Code>
              </>
            )}
            .
          </li>
        )}
        {most && (
          <li>
            Most attempts: <TicketLink id={most.ticket_id} external_key={most.external_key} />{" "}
            <Code>{most.step}</Code> ran {most.attempts} times ({most.rework} rework,{" "}
            {plural(most.retries, "retry", "retries")}),{" "}
            {formatCost(most.cost_usd, most.cost_estimated)} in all,{" "}
            {formatCost(most.rework_cost_usd, most.cost_estimated)} of it rework.
          </li>
        )}
        {gate && (
          <li>
            Longest gate wait: <TicketLink id={gate.ticket_id} external_key={gate.external_key} />{" "}
            waited {formatDuration(gate.seconds)} at <Code>{gate.step}</Code>.
          </li>
        )}
        {slowest && (
          <li>
            Slowest to done:{" "}
            <TicketLink id={slowest.ticket_id} external_key={slowest.external_key} /> took{" "}
            {formatDuration(slowest.lead_seconds)}
            {slowest.wait_step !== null && slowest.wait_seconds !== null && (
              <>
                , {formatDuration(slowest.wait_seconds)} of it at <Code>{slowest.wait_step}</Code>
              </>
            )}
            .
          </li>
        )}
        {open && (
          <li>
            Longest open wait: <TicketLink id={open.ticket_id} external_key={open.external_key} />{" "}
            has waited {formatDuration(secondsBetween(open.waiting_since, asOf))} at{" "}
            <Code>{open.step}</Code>. <NowChip />
          </li>
        )}
      </ul>
    </Panel>
  );
}
