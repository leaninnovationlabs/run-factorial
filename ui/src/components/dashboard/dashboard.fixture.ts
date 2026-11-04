import type {
  DashboardLifetime,
  OpenGate,
  PeriodFigures,
  ProjectDashboard,
  StepSegment,
  StepTime,
  TicketSpend,
} from "../../api/types";

export const PROJECT_ID = 9041;

/** Older than two 4-week windows and younger than 12 weeks: `dashboard()` has a previous period
 *  and `twelveWeeksClipped()` is clipped, the shape every young project has. */
const FIRST_ADMITTED = "2026-07-20T09:12:00Z";

const LIFETIME: DashboardLifetime = {
  spend: {
    input_tokens: 138_700_000,
    output_tokens: 1_010_000,
    cost_usd: "84.6200",
    cost_estimated: true,
  },
  tickets: { done: 11, exited: 9, open: 6, total: 26 },
  per_done: { tickets: 11, mean_usd: "5.5545", median_usd: "2.9000", cost_estimated: true },
  runs: {
    total: 96,
    priced: 80,
    estimated: 4,
    subscription: 6,
    cached_input_tokens: 123_000_000,
  },
};

const PERIOD: PeriodFigures = {
  spend: {
    input_tokens: 108_600_000,
    output_tokens: 800_000,
    cost_usd: "66.1700",
    cost_estimated: true,
  },
  runs: { total: 72, priced: 60, estimated: 4, subscription: 6, cached_input_tokens: 97_000_000 },
  tickets: { done: 8, exited: 8, admitted: 9 },
  per_done: { tickets: 8, mean_usd: "5.8000", median_usd: "2.8100", cost_estimated: true },
  velocity_per_week: 1.715,
  lead_time: { tickets: 8, median_seconds: 207_360 },
  forced_steps: 0,
};

const PREVIOUS: PeriodFigures = {
  spend: {
    input_tokens: 30_100_000,
    output_tokens: 210_000,
    cost_usd: "18.4500",
    cost_estimated: false,
  },
  runs: { total: 24, priced: 20, estimated: 0, subscription: 0, cached_input_tokens: 26_000_000 },
  tickets: { done: 3, exited: 1, admitted: 6 },
  per_done: { tickets: 3, mean_usd: "4.9000", median_usd: "3.3000", cost_estimated: false },
  velocity_per_week: 0.643,
  lead_time: { tickets: 3, median_seconds: 302_400 },
  forced_steps: 0,
};

/** From the first admission: what `all`, and a window clipped to it, both cover. */
const SINCE_FIRST: PeriodFigures = {
  spend: LIFETIME.spend,
  runs: LIFETIME.runs,
  tickets: { done: 11, exited: 9, admitted: 26 },
  per_done: LIFETIME.per_done,
  velocity_per_week: 0.947,
  lead_time: { tickets: 11, median_seconds: 241_920 },
  forced_steps: 0,
};

const LONGEST_OPEN: OpenGate = {
  ticket_id: 76,
  external_key: "76",
  title: "Add assigned reviewer to packages",
  step: "code_review_gate",
  attempt: 1,
  waiting_since: "2026-10-06T11:05:00Z",
};

const OPEN_GATES: OpenGate[] = [
  LONGEST_OPEN,
  {
    ticket_id: 88,
    external_key: "88",
    title: "Export the register as CSV",
    step: "spec_review_gate",
    attempt: 1,
    waiting_since: "2026-10-08T17:02:00Z",
  },
];

/** A 4-week window, not clipped, with the four weeks before it to compare against. */
export function dashboard(over: Partial<ProjectDashboard> = {}): ProjectDashboard {
  return {
    project_id: PROJECT_ID,
    span: "4w",
    since: "2026-09-07T00:00:00Z",
    previous_since: "2026-08-05T08:23:00Z",
    clipped: false,
    as_of: "2026-10-09T15:37:00Z",
    lifetime: LIFETIME,
    period: PERIOD,
    previous: PREVIOUS,
    open_now: 6,
    time_split: {
      tickets: 8,
      lead_seconds: 1_728_000,
      agent_seconds: 259_200,
      gate_seconds: 1_209_600,
      paused_seconds: 69_120,
      idle_seconds: 190_080,
    },
    open_gates: OPEN_GATES,
    by_outcome: [
      {
        outcome: "done",
        tickets: 8,
        cost_usd: "46.4000",
        tokens: 70_000_000,
        cost_estimated: true,
      },
      {
        outcome: "open",
        tickets: 2,
        cost_usd: "11.6700",
        tokens: 27_200_000,
        cost_estimated: false,
      },
      {
        outcome: "exited",
        tickets: 8,
        cost_usd: "8.0900",
        tokens: 12_200_000,
        cost_estimated: false,
      },
    ],
    by_ticket: [
      ticket(67, "done", "Comments feature on findings", 5, 21_400_000, "17.2100"),
      ticket(76, "waiting_gate", "Add assigned reviewer to packages", 7, 27_200_000, "11.6700"),
      ticket(77, "done", "Blocked should just be a flag", 10, 21_200_000, "11.0900"),
      ticket(86, "done", "Calculate risk level from the register", 10, 15_800_000, "8.7400", true),
      ticket(69, "done", "Edit findings screen sync", 5, 6_500_000, "3.8800"),
      ticket(60, "exited", "Comments at the findings", 4, 2_000_000, "3.4600"),
    ],
    by_ticket_rest: { tickets: 7, cost_usd: "10.1100", cost_estimated: true },
    unrun_tickets: 4,
    step_costs: [
      {
        ticket_id: 67,
        external_key: "67",
        title: "Comments feature on findings",
        cost_usd: "17.2100",
        cost_estimated: false,
        segments: [
          segment("plan", 1, "next", "4.2200", 450, 2_500_000, "2026-09-29T09:00:00Z"),
          {
            ...segment("spec_review_gate", 1, "next", "0.0000", 498, 0, "2026-09-29T09:07:30Z"),
            kind: "human_gate",
            model: null,
            status: "approved",
          },
          segment("build", 1, "next", "12.9900", 1320, 18_900_000, "2026-09-29T10:02:00Z"),
        ],
      },
      {
        ticket_id: 76,
        external_key: "76",
        title: "Add assigned reviewer to packages",
        cost_usd: "11.6700",
        cost_estimated: false,
        segments: [
          segment("plan", 1, "admission", "2.5100", 300, 3_000_000, "2026-10-05T08:00:00Z"),
          segment("plan", 2, "reject_rerun", "1.5800", 240, 2_000_000, "2026-10-05T11:30:00Z"),
          segment("build", 1, "next", "7.5900", 1920, 20_300_000, "2026-10-06T09:15:00Z", {
            parked: 3600,
          }),
        ],
      },
      {
        ticket_id: 77,
        external_key: "77",
        title: "Blocked should just be a flag",
        cost_usd: "11.0900",
        cost_estimated: false,
        segments: [
          segment("plan", 1, "next", "2.3000", 280, 2_600_000, "2026-10-01T13:00:00Z"),
          segment("build", 1, "next", "4.1000", 900, 8_000_000, "2026-10-01T15:10:00Z"),
          segment("build", 2, "retry", "0.7700", 120, 1_200_000, "2026-10-01T15:26:00Z", {
            status: "failed",
          }),
          segment("build", 3, "reject_rerun", "2.7700", 600, 6_000_000, "2026-10-02T10:00:00Z"),
          segment("build", 4, "reject_rerun", "1.1700", 300, 3_400_000, "2026-10-03T09:40:00Z"),
        ],
      },
    ],
    steps: [
      stepTime("plan", "agent", 18, 294, 540, 1_260),
      stepTime("build", "agent", 13, 618, 1_500, 1_920),
    ],
    gates: [
      stepTime("spec_review_gate", "human_gate", 13, 498, 86_400, 432_000),
      stepTime("code_review_gate", "human_gate", 11, 1734, 7_200, 187_200),
      // Under `limits.p90_min_runs`, so no p90.
      stepTime("pr_merge_gate", "human_gate", 6, 108, null, 3_600),
    ],
    unmerged: {
      rework: { count: 3, cost_usd: "5.5200", cost_estimated: false },
      exited: { count: 8, cost_usd: "8.0900", cost_estimated: false },
      failed: { count: 2, cost_usd: "0.9400", cost_estimated: false },
      set_aside: { count: 0, cost_usd: "0", cost_estimated: false },
      total_usd: "13.7800",
      cost_estimated: false,
    },
    weeks: [
      {
        week_start: "2026-09-07",
        done: 0,
        exited: 0,
        cost_usd: "0.0000",
        cost_estimated: false,
        closed: [],
      },
      {
        week_start: "2026-09-14",
        done: 3,
        exited: 1,
        cost_usd: "3.7500",
        cost_estimated: true,
        closed: [
          { ticket_id: 35, external_key: "35", outcome: "done" },
          { ticket_id: 28, external_key: "28", outcome: "exited" },
        ],
      },
      {
        week_start: "2026-09-21",
        done: 0,
        exited: 3,
        cost_usd: "0.1700",
        cost_estimated: false,
        closed: [{ ticket_id: 57, external_key: "57", outcome: "exited" }],
      },
      {
        week_start: "2026-09-28",
        done: 2,
        exited: 1,
        cost_usd: "18.9400",
        cost_estimated: false,
        closed: [
          { ticket_id: 67, external_key: "67", outcome: "done" },
          { ticket_id: 68, external_key: "68", outcome: "done" },
        ],
      },
      {
        week_start: "2026-10-05",
        done: 3,
        exited: 3,
        cost_usd: "43.3100",
        cost_estimated: false,
        closed: [
          { ticket_id: 69, external_key: "69", outcome: "done" },
          { ticket_id: 77, external_key: "77", outcome: "done" },
          { ticket_id: 86, external_key: "86", outcome: "done" },
        ],
      },
    ],
    extremes: {
      largest_run: {
        ticket_id: 67,
        external_key: "67",
        title: "Comments feature on findings",
        step: "build",
        attempt: 1,
        model: "claude-opus-4-8",
        cost_usd: "12.9900",
        cost_estimated: false,
        tokens: 18_900_000,
        seconds: 1320,
      },
      most_attempts: {
        ticket_id: 77,
        external_key: "77",
        title: "Blocked should just be a flag",
        step: "build",
        attempts: 4,
        rework: 2,
        retries: 1,
        cost_usd: "8.8100",
        rework_cost_usd: "3.9400",
        cost_estimated: false,
      },
      longest_gate: {
        ticket_id: 60,
        external_key: "60",
        title: "Comments at the findings",
        step: "code_review_gate",
        attempt: 1,
        status: "rejected",
        seconds: 187_200,
      },
      slowest_done: {
        ticket_id: 77,
        external_key: "77",
        title: "Blocked should just be a flag",
        lead_seconds: 525_600,
        wait_step: "spec_review_gate",
        wait_seconds: 432_000,
      },
      longest_open_wait: LONGEST_OPEN,
    },
    limits: { by_ticket: 5, step_costs: 5, open_gates: 5, p90_min_runs: 7 },
    ...over,
  };
}

/** `all`: from the first admission, so there is no earlier period to compare with. */
export function allTime(over: Partial<ProjectDashboard> = {}): ProjectDashboard {
  return dashboard({
    span: "all",
    since: FIRST_ADMITTED,
    period: SINCE_FIRST,
    previous: null,
    previous_since: null,
    ...over,
  });
}

/** 12 weeks on a project younger than that: `since` moved up to the first admission, and no
 *  previous period, since a part of one would compare unlike with like. */
export function twelveWeeksClipped(over: Partial<ProjectDashboard> = {}): ProjectDashboard {
  return dashboard({
    span: "12w",
    since: FIRST_ADMITTED,
    clipped: true,
    period: SINCE_FIRST,
    previous: null,
    previous_since: null,
    ...over,
  });
}

function ticket(
  id: number,
  status: TicketSpend["status"],
  title: string,
  attempts: number,
  tokens: number,
  cost_usd: string,
  cost_estimated = false,
): TicketSpend {
  return {
    ticket_id: id,
    external_key: String(id),
    title,
    status,
    attempts,
    tokens,
    cost_usd,
    cost_estimated,
  };
}

function segment(
  step: string,
  attempt: number,
  entered_by: StepSegment["entered_by"],
  cost_usd: string,
  seconds: number,
  tokens: number,
  started_at: string,
  { status = "succeeded", parked = 0 }: { status?: StepSegment["status"]; parked?: number } = {},
): StepSegment {
  return {
    step,
    kind: "agent",
    attempt,
    entered_by,
    status,
    model: "claude-opus-4-8",
    runtime_kind: "claude_cli",
    runtime_type: "claude_api",
    started_at,
    // A pause sits inside the run's start-to-end and outside its active `seconds`.
    ended_at: new Date(Date.parse(started_at) + (seconds + parked) * 1000).toISOString(),
    parked_seconds: parked,
    seconds,
    tokens,
    cost_usd,
    cost_estimated: false,
    forced_by: null,
    superseded_at: null,
  };
}

function stepTime(
  step: string,
  kind: StepTime["kind"],
  runs: number,
  median: number,
  p90: number | null,
  max: number,
): StepTime {
  return {
    process_key: "primary",
    step,
    kind,
    runs,
    median_seconds: median,
    p90_seconds: p90,
    max_seconds: max,
  };
}
