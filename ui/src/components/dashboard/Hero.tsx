/**
 * The five cards, over the window like every section under them. Two figures are not the
 * window's: Spend's "all time" line, which MCP callers read from `lifetime`, and the open count,
 * a now figure. Each change line compares with `previous`. An absent value reads as a dash,
 * never as a zero.
 */

import type { DashboardWeek, PeriodFigures, ProjectDashboard } from "../../api/types";
import { formatCost, formatDay, formatDuration, formatTokens, plural } from "../../format";
import { cx, lookStyle, StatTile } from "../ui";
import { ChangeLine } from "./ChangeLine";
import { NowChip } from "./NowChip";
import { utcDay } from "./time";

const money = (value: number) => formatCost(String(value));
const toCents = (value: number) => Number(value.toFixed(2));
const toTenths = (value: number) => Number(value.toFixed(1));
const perWeek = (value: number) => `${value.toFixed(1)} a week`;
const median = (figures: PeriodFigures) =>
  figures.per_done.median_usd === null ? null : Number(figures.per_done.median_usd);

/** Rounded as `formatLeadTime` prints it, with the same cuts as `formatDuration`: tenths of a
 *  day from a day up, whole minutes from an hour, whole seconds below. */
function roundLeadTime(seconds: number): number {
  if (seconds >= 86400) return Math.round(Number((seconds / 86400).toFixed(1)) * 10) * 8640;
  const whole = Math.round(seconds);
  return whole >= 3600 ? Math.floor(whole / 60) * 60 : whole;
}

/** In days once it reaches one, since a lead time is read in days. */
function formatLeadTime(seconds: number): string {
  return seconds >= 86400 ? `${(seconds / 86400).toFixed(1)} days` : formatDuration(seconds);
}

export function Hero({ data }: { data: ProjectDashboard }) {
  const { span, period, previous, lifetime } = data;
  const { spend, tickets, per_done: perDone, lead_time: lead } = period;
  // `undefined`, not null, when there is no earlier period: the change line says so in words.
  const before = (pick: (figures: PeriodFigures) => number | null) =>
    previous ? pick(previous) : undefined;
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-5">
        <StatTile
          label="Spend"
          // The sum coalesces unpriced runs to zero, as the dashboard header's figure does (`ProjectDashboardPage`).
          value={formatCost(
            Number(spend.cost_usd) === 0 ? null : spend.cost_usd,
            spend.cost_estimated,
          )}
          change={
            <ChangeLine
              span={span}
              current={Number(spend.cost_usd)}
              previous={before((figures) => Number(figures.spend.cost_usd))}
              format={money}
              round={toCents}
              better={null}
            />
          }
          caption={
            span === "all"
              ? formatTokens(spend.input_tokens, spend.output_tokens)
              : `${formatTokens(spend.input_tokens, spend.output_tokens)} · ${formatCost(lifetime.spend.cost_usd, lifetime.spend.cost_estimated)} all time`
          }
        />
        <StatTile
          label="Cost per done ticket"
          value={formatCost(perDone.median_usd, perDone.cost_estimated)}
          change={
            <ChangeLine
              span={span}
              current={median(period)}
              previous={before(median)}
              format={money}
              round={toCents}
              better="down"
            />
          }
          caption={`mean ${formatCost(perDone.mean_usd, perDone.cost_estimated)} · ${plural(perDone.tickets, "ticket")}`}
        />
        <StatTile
          label="Tickets done"
          value={String(tickets.done)}
          change={
            <ChangeLine
              span={span}
              current={tickets.done}
              previous={before((figures) => figures.tickets.done)}
              format={String}
              better="up"
            />
          }
          caption={
            <>
              {tickets.exited} exited · {tickets.admitted} admitted · {data.open_now} open{" "}
              <NowChip />
            </>
          }
        />
        <StatTile
          label="Velocity"
          value={period.velocity_per_week === null ? null : perWeek(period.velocity_per_week)}
          aside={<DoneSparkline weeks={data.weeks} />}
          change={
            <ChangeLine
              span={span}
              current={period.velocity_per_week}
              previous={before((figures) => figures.velocity_per_week)}
              format={perWeek}
              round={toTenths}
              better="up"
            />
          }
          caption={`${tickets.done} done since ${formatDay(utcDay(data.since))}${
            period.forced_steps > 0 ? ` · ${period.forced_steps} forced` : ""
          }`}
        />
        <StatTile
          label="Lead time"
          value={lead.median_seconds === null ? null : formatLeadTime(lead.median_seconds)}
          change={
            <ChangeLine
              span={span}
              current={lead.median_seconds}
              previous={before((figures) => figures.lead_time.median_seconds)}
              format={formatLeadTime}
              round={roundLeadTime}
              better="down"
            />
          }
          caption={`admission to done · ${plural(lead.tickets, "ticket")}`}
        />
      </div>
    </div>
  );
}

/** Done per week, each bar a share of the busiest week; the caption carries the total and the
 *  image's name every week. */
function DoneSparkline({ weeks }: { weeks: DashboardWeek[] }) {
  if (weeks.length < 2) return null;
  const top = Math.max(1, ...weeks.map((week) => week.done));
  return (
    <span
      role="img"
      aria-label={`Done by week: ${weeks.map((week) => `${formatDay(week.week_start)} ${week.done}`).join(", ")}`}
      className="flex h-6 w-20 items-end gap-px self-center"
    >
      {weeks.map((week, index) => (
        <span
          key={week.week_start}
          style={{ height: `${(week.done / top) * 100}%`, ...lookStyle("outcome-done") }}
          // The last week is the one `as_of` falls in, so it is not over yet.
          className={cx(
            "min-h-0.5 min-w-px flex-1 rounded-xs bg-[var(--look)]",
            index === weeks.length - 1 && "opacity-disabled",
          )}
        />
      ))}
    </span>
  );
}
