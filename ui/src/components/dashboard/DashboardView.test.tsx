/**
 * Every figure the dashboard route serves reaches the markup, and the cards' reach it once. A
 * section that silently draws from the wrong field, or a refactor that drops one, is the
 * regression this tab can have without anyone noticing — the figures still look like figures.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { keys } from "../../api/queries";
import type { ProjectDashboard } from "../../api/types";
import { formatDay } from "../../format";
import { type DashboardSection, DashboardView } from "./DashboardView";
import { allTime, dashboard, PROJECT_ID, twelveWeeksClipped } from "./dashboard.fixture";

function render(data: ProjectDashboard = dashboard(), section: DashboardSection = "spend"): string {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, retryOnMount: false } },
  });
  qc.setQueryData(keys.dashboard(PROJECT_ID, "all"), data);
  return renderToString(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <DashboardView projectId={PROJECT_ID} section={section} onSectionChange={() => {}} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const plainText = (html: string) =>
  html
    .replace(/<!-- -->/g, "")
    .replace(/<[^>]+>/g, "")
    .replace(/\s+/g, " ");

const times = (html: string, text: string) => html.split(text).length - 1;

const changeLines = (html: string) =>
  [...html.matchAll(/<p data-change="(\w+)"[^>]*>(.*?)<\/p>/g)].map(
    ([, tone, body]) => `${tone}: ${plainText(body ?? "").trim()}`,
  );

// Panels that carry a note when the fixture is drawn, per sub-tab.
const NOTED_PANELS: Record<DashboardSection, number> = { spend: 4, time: 2, activity: 2 };

// Every sub-tab's panels in one string, for a figure that may sit in any of them.
const renderSections = (data: ProjectDashboard = dashboard()) =>
  (["spend", "time", "activity"] as const).map((section) => render(data, section)).join("");

describe("DashboardView", () => {
  it("splits done tickets' time three ways, waiting split by gate and pause, and says when none finished", () => {
    const split = {
      tickets: 3,
      lead_seconds: 360_600,
      agent_seconds: 94_200,
      gate_seconds: 180_000,
      paused_seconds: 15_000,
      idle_seconds: 71_400,
    };
    const html = render(dashboard({ time_split: split }), "time");
    expect(html).toContain("Agents working 26% · 1d 2h");
    expect(html).toContain("Waiting for people 54% · 2d 6h (at gates 50%, paused mid-run 4%)");
    expect(html).toContain("Idle 20% · 19h 50m");
    const none = {
      tickets: 0,
      lead_seconds: 0,
      agent_seconds: 0,
      gate_seconds: 0,
      paused_seconds: 0,
      idle_seconds: 0,
    };
    expect(plainText(render(dashboard({ time_split: none }), "time"))).toContain(
      "No ticket is done yet",
    );
  });

  it("draws every served figure, and each card figure once", () => {
    const cards = render();
    for (const card of [
      "$66.17",
      "$84.62 all time",
      "108.6M in / 800K out",
      "$2.81",
      "mean ~$5.80 · 8 tickets",
      "1.7 a week",
      "8 done since Sep 7",
      "2.4 days",
      "admission to done · 8 tickets",
    ]) {
      expect(times(cards, card), card).toBe(1);
    }
    expect(plainText(cards)).toContain("8 exited · 9 admitted · 6 open Now");
    const html = renderSections();
    const served = [
      // by outcome
      "$46.40",
      "$11.67",
      "$8.09",
      // by ticket, the fold and the un-run count
      "$17.21",
      "$11.09",
      "$8.74",
      "$3.88",
      "$3.46",
      "$10.11",
      "7 more tickets",
      "4 with no runs",
      // by attempt
      "$4.22",
      "$12.99",
      "$2.51",
      "$1.58",
      "$7.59",
      "$2.30",
      "$4.10",
      "$0.77",
      "$2.77",
      "$1.17",
      // steps and gates: the medians
      "4m 54s",
      "10m 18s",
      "8m 18s",
      "28m 54s",
      "1m 48s",
      // spend that produced no merged change
      "$5.52",
      "$0.94",
      "$13.78",
      // weeks
      "Sep 14",
      "Oct 5 (partial)",
      "$43.31",
      "$18.94",
      "$3.75",
      "$0.17",
      // the callouts: largest run, most attempts, longest gate
      "22m 0s",
      "18.9M",
      "claude-opus-4-8",
      "$8.81",
      "$3.94",
      "2d 4h",
    ];
    for (const figure of served) expect(html, figure).toContain(figure);
    // Only the week `as_of` falls in is unfinished.
    expect(times(html, "(partial)"), "the current week").toBe(1);
    // A gate costs nothing and is zero-width: not a segment, not a row.
    expect(html).not.toContain("spec_review_gate attempt 1");
    // The gate segment's 498 s is the gate median too: a gate row in the attempt table adds a second.
    expect(times(html, "8m 18s"), "gate median").toBe(1);
    // $2.51 is only #76's first plan attempt: its tip. Its table row is in a drawer, shut here.
    expect(times(html, "$2.51"), "plan attempt 1 of #76").toBe(1);
    expect(plainText(html)).toContain(
      "Largest single run: #67 build attempt 1, $12.99 in 22m 0s, 18.9M tokens on claude-opus-4-8.",
    );
  });

  it("names the time outliers and the gates waiting now", () => {
    const base = dashboard();
    const waiting = {
      ticket_id: 76,
      external_key: "76",
      title: "Add assigned reviewer to packages",
      step: "code_review_gate",
      attempt: 1,
      waiting_since: "2026-10-05T08:37:00Z",
    };
    const text = plainText(
      renderSections(
        dashboard({
          open_gates: [waiting],
          extremes: {
            ...base.extremes,
            slowest_done: {
              ticket_id: 77,
              external_key: "77",
              title: "Blocked should just be a flag",
              lead_seconds: 525_600,
              wait_step: "code_review_gate",
              wait_seconds: 446_400,
            },
            longest_open_wait: waiting,
          },
        }),
      ),
    );
    expect(text).toContain("Slowest to done: #77 took 6d 2h, 5d 4h of it at code_review_gate.");
    // `as_of` is 2026-10-09T15:37Z: the wait is measured on the served clock, not the browser's.
    expect(text).toContain("Longest open wait: #76 has waited 4d 7h at code_review_gate.");
    expect(text).toContain("Longest gate wait: #60 waited 2d 4h at code_review_gate.");
    expect(text).toMatch(/Waiting now.*#76 code_review_gate attempt 1, waiting 4d 7h\./);
  });

  it("draws a dash for a figure with nothing behind it, never NaN", () => {
    const nothing = {
      ...dashboard().period,
      tickets: { done: 0, exited: 0, admitted: 0 },
      per_done: { tickets: 0, mean_usd: null, median_usd: null, cost_estimated: false },
      velocity_per_week: null,
      lead_time: { tickets: 0, median_seconds: null },
    };
    const html = render(allTime({ period: nothing }));
    expect(plainText(html)).not.toMatch(/NaN|Infinity/);
    // Cost per done ticket, velocity and lead time. The done count is a real zero, so a 0.
    expect(html.match(/data-stat-value[^>]*>-</g)).toHaveLength(3);
  });

  it("keeps a ticket title as text", () => {
    const hostile = dashboard();
    const [first] = hostile.by_ticket;
    if (!first) throw new Error("the fixture has no tickets");
    hostile.by_ticket[0] = { ...first, title: "<img src=x onerror=alert(1)>" };
    const html = render(hostile);
    expect(html).toContain("&lt;img src=x onerror=alert(1)&gt;");
    expect(html).not.toContain("<img");
    expect(html).toMatch(/<span class="truncate text-subtle sm:sr-only">[^<]* · &lt;img/);
  });

  it("draws a restart attempt in Spend by step, rather than failing to draw", () => {
    const data = dashboard();
    const [first] = data.step_costs;
    const [plan] = first?.segments ?? [];
    if (!first || !plan) throw new Error("the fixture has no step costs");
    data.step_costs[0] = {
      ...first,
      segments: [
        ...first.segments,
        { ...plan, attempt: 2, entered_by: "restart", cost_usd: "0.6100" },
      ],
    };
    expect(render(data)).toContain('data-tip="plan attempt 2: $0.61"');
  });

  it("pairs the cost lists at xl only when Spend by step has rows", () => {
    expect(render(dashboard())).toContain("xl:grid-cols-2");
    expect(render(dashboard({ step_costs: [] }))).not.toContain("xl:grid-cols-2");
  });

  it("counts a span's outcome tickets, and names the un-run count as lifetime", () => {
    const span = render();
    expect(span).toContain("Done, 8 tickets");
    expect(span).toContain("4 with no runs, all time");
    expect(render(allTime())).not.toContain("with no runs, all time");
  });

  it("draws the costliest ticket full width however much the fold sums to", () => {
    const rest = { tickets: 40, cost_usd: "90.0000", cost_estimated: false };
    const html = render(dashboard({ by_ticket_rest: rest }));
    const [first] = [...html.matchAll(/data-bar="[^"]*"[^>]*style="width:([^;"]+)/g)];
    expect(first?.[1]).toBe("100%");
  });

  it("dates the velocity caption by the UTC day", () => {
    const text = plainText(render(dashboard({ since: "2026-09-06T22:00:00-05:00" })));
    expect(text).toContain(`done since ${formatDay("2026-09-07")}`);
  });

  it("does not repeat the spend as all time under all time", () => {
    const html = render(allTime());
    expect(html).not.toContain("all time</");
    expect(plainText(html)).not.toMatch(/out · \$[\d.,]+ all time/);
  });

  it.each(["spend", "time", "activity"] as const)(
    "draws each method note on the %s sub-tab behind an info icon, with no methodology section or note strip",
    (section) => {
      const html = render(dashboard(), section);
      expect(html).not.toContain("How these figures are made");
      expect(html.match(/data-panel-note/g)).toHaveLength(NOTED_PANELS[section]);
      expect(html).not.toMatch(/<p[^>]*border-t[^>]*>/);
    },
  );

  it("offers the three sub-tabs, and none in an empty state", () => {
    const html = render();
    expect(html).toMatch(
      /role="tablist"[^>]*aria-label="Dashboard sections"|aria-label="Dashboard sections"[^>]*role="tablist"/,
    );
    expect(html.match(/role="tab"/g)).toHaveLength(3);
    const base = dashboard();
    const never = dashboard({
      lifetime: { ...base.lifetime, runs: { ...base.lifetime.runs, total: 0 } },
    });
    expect(render(never)).not.toContain('aria-label="Dashboard sections"');
  });

  describe("the change line", () => {
    it("states each card's change against the prior period, coloured by what it means", () => {
      expect(changeLines(render())).toEqual([
        "neutral: Up $47.72 compared with the previous 4 weeks",
        "good: Down $0.49 compared with the previous 4 weeks",
        "good: Up 5 compared with the previous 4 weeks",
        "good: Up 1.1 a week compared with the previous 4 weeks",
        "good: Down 1.1 days compared with the previous 4 weeks",
      ]);
    });

    it("says there is no earlier period rather than comparing with part of one", () => {
      const lines = changeLines(render(twelveWeeksClipped()));
      expect(lines).toHaveLength(5);
      for (const line of lines) expect(line).toBe("muted: Nothing to compare with yet");
    });

    it("states the change between the figures as printed when they straddle a rounding step", () => {
      const base = dashboard();
      const previous = base.previous;
      if (!previous) throw new Error("fixture has a previous period");
      const lines = changeLines(
        render(
          dashboard({
            period: {
              ...base.period,
              velocity_per_week: 1.76,
              spend: { ...base.period.spend, cost_usd: "18.896" },
            },
            previous: {
              ...previous,
              velocity_per_week: 1.74,
              spend: { ...previous.spend, cost_usd: "18.894" },
            },
          }),
        ),
      );
      expect(lines[0]).toBe("neutral: Up $0.01 compared with the previous 4 weeks");
      expect(lines[3]).toBe("good: Up 0.1 a week compared with the previous 4 weeks");
    });

    it("reads the change off the figures exactly as the headlines round them", () => {
      const lines = (c: number, p: number, kind: "spend" | "velocity" | "lead") => {
        const base = dashboard();
        const previous = base.previous;
        if (!previous) throw new Error("fixture has a previous period");
        const set = (f: typeof base.period, v: number): typeof base.period =>
          kind === "spend"
            ? { ...f, spend: { ...f.spend, cost_usd: String(v) } }
            : kind === "velocity"
              ? { ...f, velocity_per_week: v }
              : { ...f, lead_time: { ...f.lead_time, median_seconds: v } };
        const all = changeLines(
          render(dashboard({ period: set(base.period, c), previous: set(previous, p) })),
        );
        return all[kind === "spend" ? 0 : kind === "velocity" ? 3 : 4];
      };
      expect(lines(18.895, 18.89, "spend")).toBe("neutral: Same as the previous 4 weeks");
      expect(lines(1.15, 1.1, "velocity")).toBe("neutral: Same as the previous 4 weeks");
      expect(lines(263520, 259200, "lead")).toBe("neutral: Same as the previous 4 weeks");
      expect(lines(18630, 18000, "lead")).toBe("bad: Up 10m 0s compared with the previous 4 weeks");
      expect(lines(14410, 14390, "lead")).toBe("bad: Up 1m 0s compared with the previous 4 weeks");
      expect(lines(1.1 * 86400, 2.1 * 86400, "lead")).toBe(
        "good: Down 1.0 days compared with the previous 4 weeks",
      );
      expect(lines(0.004, 0, "spend")).toBe(
        "neutral: Up &lt;$0.01 compared with the previous 4 weeks",
      );
    });

    it("draws none under all time", () => {
      expect(changeLines(render(allTime()))).toEqual([]);
    });

    it("says same when the two figures print alike, whatever the unrounded delta", () => {
      const base = dashboard();
      const previous = base.previous;
      if (!previous) throw new Error("fixture has a previous period");
      const lines = changeLines(
        render(
          dashboard({
            period: {
              ...base.period,
              velocity_per_week: 1.74,
              lead_time: { ...base.period.lead_time, median_seconds: 2.41 * 86400 },
              spend: { ...base.period.spend, cost_usd: "18.904" },
            },
            previous: {
              ...previous,
              velocity_per_week: 1.66,
              lead_time: { ...previous.lead_time, median_seconds: 2.39 * 86400 },
              spend: { ...previous.spend, cost_usd: "18.896" },
            },
          }),
        ),
      );
      expect(lines[0]).toBe("neutral: Same as the previous 4 weeks");
      expect(lines[3]).toBe("neutral: Same as the previous 4 weeks");
      expect(lines[4]).toBe("neutral: Same as the previous 4 weeks");
    });
  });
});
