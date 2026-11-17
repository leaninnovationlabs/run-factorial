/**
 * The ticket page's fixture: one ticket at a gate after a failed first attempt, its log, its
 * pinned config and its project, seeded straight into a query cache so a test renders real
 * content without a fetch.
 */

import { QueryClient } from "@tanstack/react-query";
import { keys } from "../api/queries";
import { seedFailure } from "../api/queryTesting";

export const TICKET_ID = 5204;
export const PROJECT_ID = 9041;

export const CONFIG = {
  entry: { step: "plan" },
  steps: [
    {
      name: "plan",
      kind: "agent",
      next: "spec_gate",
      preconditions: { all: ["ticket_has_assignee()"] },
    },
    {
      name: "spec_gate",
      kind: "human_gate",
      next: "done",
      gate: { onReject: { rerunStep: "plan" } },
    },
  ],
  terminal: { step: "done" },
};

const PINNED_SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0";

export function run(id: number, step_name: string, attempt: number, status: string, extra = {}) {
  return {
    id,
    step_name,
    kind: "agent",
    attempt,
    status,
    failure_reason: null,
    decided_by: null,
    runtime_run_id: null,
    runtime_kind: null,
    model: null,
    input_tokens: null,
    cached_input_tokens: null,
    output_tokens: null,
    cost_usd: null,
    cost_source: null,
    started_at: null,
    ended_at: null,
    created_at: "2026-08-25T09:00:00Z",
    generation: 1,
    mergeability: null,
    base_ref: null,
    can_fix_conflict: false,
    conflict_fix_pending: false,
    conflict_send_back: false,
    ...extra,
  };
}

/** Extra timeline entries a single test needs, kept out of the shared fixture. */
export type Entry = {
  id: number;
  type: string;
  details: Record<string, unknown>;
  created_at: string;
};

/** The ticket row, overridable so a test can put the same page in another state. */
export type TicketOver = Partial<{
  current_step: string;
  status: string;
  step_runs: unknown[];
  title: string | null;
  quarantined_at: string | null;
  consecutive_failures: number;
  park_reason: string | null;
  generation: number;
  lane_head: { process_version_id: number; sha: string } | null;
  waiting_on: string[];
}>;

/** The live issue read: GitHub's `state`, or a read that failed. */
export type IssueRead = string | { error: string };

export function seeded(
  extra: Entry[] = [],
  over: TicketOver = {},
  issueState?: IssueRead,
  config: Record<string, unknown> = CONFIG,
) {
  // `retryOnMount: false`, so a failed read seeded below is what the first paint sees rather than
  // a retry the server renderer would never run.
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, retryOnMount: false } },
  });
  qc.setQueryData(keys.ticket(TICKET_ID), {
    id: TICKET_ID,
    project_id: PROJECT_ID,
    external_key: "204",
    title: "Remit-to address is dropped on re-issued invoices",
    process_version_id: 17,
    process_sha: PINNED_SHA,
    lane_head: { process_version_id: 17, sha: PINNED_SHA },
    generation: 1,
    owner_at_admission: "operator",
    current_step: "spec_gate",
    status: "waiting_gate",
    admitted_at: "2026-08-25T09:02:41Z",
    closed_at: null,
    quarantined_at: null,
    step_runs: [
      run(1, "plan", 1, "failed", {
        failure_reason: 'Postcondition not met: branch_exists("spec/204-*") is false.',
        runtime_run_id: "204-plan-agent-7f3a2c",
        started_at: "2026-08-25T09:05:00Z",
        ended_at: "2026-08-25T09:24:18Z",
      }),
      run(2, "plan", 2, "succeeded", {
        runtime_run_id: "204-plan-agent-bb09c1",
        cost_usd: "0.94410000",
        cost_source: "reported",
        input_tokens: 41880,
        output_tokens: 9204,
        started_at: "2026-08-25T09:34:00Z",
        ended_at: "2026-08-25T10:12:33Z",
      }),
      // A run whose events have not arrived on the timeline poll yet. No `started_at`, so the
      // header's total stays a fixed number rather than one that counts up as the suite runs.
      run(3, "spec_gate", 1, "waiting", { kind: "human_gate" }),
    ],
    ...over,
  });
  qc.setQueryData(keys.timeline(TICKET_ID), {
    ticket_id: TICKET_ID,
    entries: [
      {
        id: 1,
        type: "ticket_admitted",
        details: { step: "plan", owner: "operator" },
        created_at: "2026-08-25T09:02:41Z",
      },
      {
        id: 2,
        type: "step_started",
        details: { step: "plan", attempt: 1, kind: "agent" },
        created_at: "2026-08-25T09:05:00Z",
      },
      {
        id: 3,
        type: "agent_spawned",
        details: {
          step: "plan",
          attempt: 1,
          agent: "plan-agent",
          run_id: "204-plan-agent-7f3a2c",
          runtime: "claude_cli",
          branch: "spec/204-remit-to",
        },
        created_at: "2026-08-25T09:05:04Z",
      },
      {
        id: 4,
        type: "postcondition_failed",
        details: { step: "plan", attempt: 1, predicate: 'branch_exists("spec/204-*")' },
        created_at: "2026-08-25T09:24:18Z",
      },
      {
        id: 5,
        type: "retry_scheduled",
        // The worker's own shape: the attempt that failed, the one being booked, and when the
        // backoff expires (`worker/failures.py`).
        details: {
          step: "plan",
          attempt: 1,
          next_attempt: 2,
          max_attempts: 2,
          due_at: "2026-08-25T09:34:18+00:00",
        },
        created_at: "2026-08-25T09:24:18Z",
      },
      {
        id: 6,
        type: "agent_completed",
        details: {
          step: "plan",
          attempt: 2,
          summary: `${"x".repeat(400)}\n\nNeeds a decision: remit-to address.`,
        },
        created_at: "2026-08-25T10:12:33Z",
      },
      ...extra,
    ],
  });
  qc.setQueryData(keys.ticketProcess(TICKET_ID, 17), {
    ticket_id: TICKET_ID,
    process_version_id: 17,
    process_key: "standard",
    source_commit_sha: PINNED_SHA,
    content: config,
  });
  qc.setQueryData(keys.project(PROJECT_ID), {
    id: PROJECT_ID,
    name: "Harbor invoices",
    github_repo: "northwind/harbor-invoices",
    description: null,
    settings: {},
    active: true,
    created_at: "2026-07-02T14:18:03Z",
    ticket_source: null,
    ticket_count: 1,
    open_ticket_count: 1,
    secret_names: [],
    blockers: [],
    warnings: [],
  });
  // The live issue read, seeded only where a test is about it: absent, the query is still
  // pending, which is the state `TicketActions` has to hold its Re-enter button through.
  if (typeof issueState === "object") {
    seedFailure(qc, keys.ticketIssue(TICKET_ID), issueState.error);
  } else if (issueState !== undefined) {
    qc.setQueryData(keys.ticketIssue(TICKET_ID), {
      ticket_id: TICKET_ID,
      repo: "northwind/harbor-invoices",
      number: 204,
      title: "Remit-to address is dropped on re-issued invoices",
      body: "",
      state: issueState,
    });
  }
  return qc;
}
