import { afterEach, describe, expect, it, vi } from "vitest";
import type { StepRun, TimelineEntry } from "../../api/types";
import { forgeHost } from "../../forge";
import { run as fixtureRun } from "../../pages/TicketPage.fixture";
import { ENTRY_NODE_ID } from "../flow/processGraph";
import {
  type ActivitySegment,
  attemptKeyForNode,
  currentRows,
  factsFor,
  gatePullNumber,
  heldAt,
  linkFor,
  linkPulse,
  segmentsFrom,
  snippet,
  sortSegments,
  statusDot,
  totalsFrom,
  waitingGatePr,
} from "./activity";
import { cancelledRetries } from "./retry";

describe("gatePullNumber", () => {
  const entry = (id: number, type: string, details: Record<string, unknown>): TimelineEntry => ({
    id,
    type,
    details,
    created_at: "2026-08-25T10:00:00Z",
  });

  it("prefers the live forge number over the frozen gate_waiting event", () => {
    const entries = [entry(1, "gate_waiting", { step: "spec_gate", pr_number: 3 })];
    expect(gatePullNumber(8, entries, "spec_gate")).toBe("8");
  });

  it("falls back to the timeline when live observation is empty", () => {
    const entries = [entry(1, "gate_waiting", { step: "spec_gate", pr_number: 248 })];
    expect(gatePullNumber(null, entries, "spec_gate")).toBe("248");
    expect(gatePullNumber(undefined, entries, "spec_gate")).toBe("248");
  });
});

describe("waitingGatePr", () => {
  const entry = (id: number, type: string, details: Record<string, unknown>): TimelineEntry => ({
    id,
    type,
    details,
    created_at: "2026-08-25T10:00:00Z",
  });

  it("finds the pull request the current gate is waiting on, newest first", () => {
    const entries = [
      entry(1, "gate_waiting", { step: "spec_gate", pr_number: 240 }),
      entry(2, "gate_rejected", { step: "spec_gate", pr_number: 240 }),
      entry(3, "gate_waiting", { step: "spec_gate", pr_number: 248 }),
      entry(4, "gate_waiting", { step: "impl_gate", pr_number: 251 }),
    ];
    expect(waitingGatePr(entries, "spec_gate")).toBe("248");
    expect(waitingGatePr(entries, "impl_gate")).toBe("251");
  });

  it("says nothing for a gate that has named no pull request", () => {
    expect(
      waitingGatePr([entry(1, "gate_waiting", { step: "spec_gate" })], "spec_gate"),
    ).toBeNull();
    expect(waitingGatePr([], "spec_gate")).toBeNull();
  });
});

const REPO = "northwind/harbor-invoices";

afterEach(() => {
  vi.useRealTimers();
});

function ev(id: number, type: string, details: Record<string, unknown> = {}): TimelineEntry {
  return { id, type, details, created_at: "2026-08-25T09:00:00Z" };
}

function run(
  step_name: string,
  attempt: number,
  status: StepRun["status"],
  extra: Partial<StepRun> = {},
): StepRun {
  return fixtureRun(attempt * 100, step_name, attempt, status, extra) as StepRun;
}

describe("segmentsFrom", () => {
  it("splits one step's retries into separate blocks", () => {
    const segments = segmentsFrom(
      [
        ev(1, "step_started", { step: "plan", attempt: 1 }),
        ev(2, "postcondition_failed", { step: "plan", attempt: 1 }),
        ev(3, "step_started", { step: "plan", attempt: 2 }),
        ev(4, "step_completed", { step: "plan", attempt: 2 }),
      ],
      [run("plan", 1, "failed"), run("plan", 2, "succeeded")],
    );

    expect(segments.map((s) => s.key)).toEqual(["plan#1", "plan#2"]);
    // Both point at the same graph node: the graph is drawn from config and has no attempts.
    expect(segments.every((s) => s.nodeId === "plan")).toBe(true);
    expect(segments.map((s) => s.status)).toEqual(["failed", "succeeded"]);
  });

  it("pins admission to the synthetic entry node with no attempt", () => {
    const segments = segmentsFrom([ev(1, "ticket_admitted", { step: "plan" })], []);

    expect(segments[0]?.nodeId).toBe(ENTRY_NODE_ID);
    expect(segments[0]?.attempt).toBeNull();
    expect(segments[0]?.run).toBeNull();
  });

  it("opens the next attempt's block with the retry that scheduled it", () => {
    // The worker writes BOTH attempts on this event — `attempt` is the one that just failed,
    // `next_attempt` the one being booked — and the block it opens is the booked one. Reading
    // `attempt` filed the retry under the failure it followed instead.
    const segments = segmentsFrom(
      [
        ev(1, "postcondition_failed", { step: "plan", attempt: 1 }),
        ev(2, "retry_scheduled", {
          step: "plan",
          attempt: 1,
          next_attempt: 2,
          max_attempts: 2,
          due_at: "2026-08-25T09:34:18+00:00",
        }),
        ev(3, "step_started", { step: "plan", attempt: 2 }),
      ],
      [],
    );

    expect(segments.map((s) => s.entries.map((e) => e.id))).toEqual([[1], [2, 3]]);
  });

  it("opens the block for the attempt an un-block granted, not the one that failed", () => {
    // `_grant_attempt` grants attempt 3 and names it in `next_attempt`, exactly as
    // `retry_scheduled` names the attempt it books. Filed under `attempt` — the exhausted run —
    // the un-block joins the FAILED block and attempt 3 has nothing on the page until
    // `step_started`, which is a poll interval away at best and the rest of the failed run's
    // backoff away at worst.
    const segments = segmentsFrom(
      [
        ev(1, "agent_failed", { step: "plan", attempt: 2 }),
        ev(2, "escalated", { step: "plan", attempt: 2 }),
        ev(3, "unblocked", {
          step: "plan",
          attempt: 2,
          next_attempt: 3,
          due_at: "2026-08-25T09:34:18+00:00",
        }),
      ],
      [run("plan", 2, "failed")],
    );

    expect(segments.map((s) => s.key)).toEqual(["plan#2", "plan#3"]);
    expect(segments[1]?.entries.map((e) => e.id)).toEqual([3]);
  });

  it("keeps blocks in log order, including a step revisited after a gate rejection", () => {
    const segments = segmentsFrom(
      [
        ev(1, "step_completed", { step: "plan", attempt: 1 }),
        ev(2, "gate_rejected", { step: "spec_gate", attempt: 1 }),
        ev(3, "step_started", { step: "plan", attempt: 2 }),
      ],
      [],
    );

    expect(segments.map((s) => s.key)).toEqual(["plan#1", "spec_gate#1", "plan#2"]);
  });

  it("still shows a run whose events have not been fetched yet", () => {
    // Dropping it would hide a step run from the only view of the ticket.
    const segments = segmentsFrom(
      [ev(1, "step_started", { step: "plan", attempt: 1 })],
      [run("plan", 1, "succeeded"), run("build", 1, "running")],
    );

    expect(segments.map((s) => s.key)).toEqual(["plan#1", "build#1"]);
    expect(segments[1]?.entries).toEqual([]);
    expect(segments[1]?.status).toBe("running");
  });

  it("orders several event-less runs by when they were created", () => {
    const segments = segmentsFrom(
      [],
      [
        run("test", 1, "running", { created_at: "2026-08-25T12:00:00Z" }),
        run("build", 1, "succeeded", { created_at: "2026-08-25T09:00:00Z" }),
      ],
    );

    expect(segments.map((s) => s.key)).toEqual(["build#1", "test#1"]);
  });

  it("survives an event with no step, rather than dropping it", () => {
    const segments = segmentsFrom([ev(1, "unblocked", {})], []);
    expect(segments).toHaveLength(1);
    expect(segments[0]?.step).toBe("unknown");
    expect(segments[0]?.entries).toHaveLength(1);
  });
});

describe("factsFor", () => {
  it("folds an unbounded summary into the modal and truncates what stays in the row", () => {
    const long = `${"a".repeat(400)}\n\nsecond paragraph`;
    const facts = factsFor(ev(1, "agent_completed", { step: "plan", attempt: 1, summary: long }));

    expect(facts.long?.body).toBe(long);
    expect(facts.primary?.length).toBeLessThanOrEqual(120);
    expect(facts.primary?.endsWith("…")).toBe(true);
    expect(facts.tone).toBe("success");
    expect(facts.significant).toBe(true);
  });

  it("gives a short summary no truncation", () => {
    const facts = factsFor(ev(1, "agent_completed", { summary: "Opened PR #248." }));
    expect(facts.primary).toBe("Opened PR #248.");
    expect(facts.long?.body).toBe("Opened PR #248.");
  });

  it("marks step bookkeeping as insignificant so it can be de-emphasised", () => {
    expect(factsFor(ev(1, "step_started", { step: "plan", attempt: 1 })).significant).toBe(false);
    expect(factsFor(ev(2, "step_completed", { step: "plan", attempt: 1 })).significant).toBe(false);
    expect(factsFor(ev(3, "postcondition_failed", { failures: ["x"] })).significant).toBe(true);
  });

  it("puts the first failure in the row and every failure in the modal, on a row from before needs", () => {
    const facts = factsFor(
      ev(1, "postcondition_failed", {
        step: "plan",
        attempt: 1,
        failures: ['branch_exists("spec/204-*")', 'pr_open("spec/204-*", "main")'],
      }),
    );
    expect(facts.label).toBe("Expected results missing");
    expect(facts.primary).toBe('branch_exists("spec/204-*")');
    expect(facts.long?.title).toBe("Expected results missing");
    expect(facts.long?.body).toBe('branch_exists("spec/204-*")\npr_open("spec/204-*", "main")');
    expect(facts.tone).toBe("error");
  });

  it("words what is missing in the row, and folds the rules it checked under the words", () => {
    const facts = factsFor(
      ev(1, "postcondition_failed", {
        step: "plan",
        attempt: 1,
        failures: ['branch_exists("spec/204-*") = false', 'pr_open("spec/204-*", "main") = false'],
        needs: ["the branch spec/204-…", "an open pull request from spec/204-… into main"],
      }),
    );
    expect(facts.primary).toBe("the branch spec/204-…");
    expect(facts.long).toEqual({
      title: "Expected results missing",
      body: 'the branch spec/204-…\nan open pull request from spec/204-… into main\n\nRules checked:\nbranch_exists("spec/204-*") = false\npr_open("spec/204-*", "main") = false',
    });
  });

  it.each(["agent_failed", "action_failed", "check_failed"])(
    "puts a %s row's plain summary in the row and its full reason in the modal",
    (type) => {
      const reason =
        "misconfiguration: the runtime has no key\n\nTraceback (most recent call last):";
      const facts = factsFor(
        ev(1, type, { failure_reason: reason, failure_summary: "Setup problem: no API key" }),
      );
      expect(facts.primary).toBe("Setup problem: no API key");
      expect(facts.long).toEqual({ title: "Failure details", body: reason });
    },
  );

  it("falls back to the reason's first paragraph on a failure row from before summaries", () => {
    const reason = "the agent exited with code 1\n\nstderr: boom";
    const facts = factsFor(ev(1, "agent_failed", { failure_reason: reason }));
    expect(facts.primary).toBe("the agent exited with code 1");
    expect(facts.long?.body).toBe(reason);
  });

  it("prefers an escalation's own reason, then the failure's summary, then its raw reason", () => {
    const raw = 'branch_exists("spec/204-*") = false';
    const summary = "Expected results missing: a branch starting with spec/204-";
    expect(
      factsFor(ev(1, "escalated", { reason: "rejected twice", failure_summary: summary })).primary,
    ).toBe("rejected twice");
    const summarised = factsFor(
      ev(1, "escalated", { failure_reason: raw, failure_summary: summary }),
    );
    expect(summarised.primary).toBe(summary);
    expect(summarised.long?.body).toBe(raw);
    expect(factsFor(ev(1, "escalated", { failure_reason: raw })).primary).toBe(raw);
  });

  it("words a sandbox outage plainly and keeps the exception behind the modal", () => {
    const error = "e2b.exceptions.TimeoutException: 502 Bad Gateway";
    const facts = factsFor(ev(1, "substrate_unavailable", { error }));
    expect(facts.label).toBe("Sandbox service unavailable");
    expect(facts.primary).toBe("The sandbox service couldn't be reached. Factorial keeps trying.");
    expect(facts.long).toEqual({ title: "Error details", body: error });
    expect(facts.tone).toBe("attention");
  });

  it("words what the next step waits for, falling back to the rules on an older row", () => {
    const worded = factsFor(
      ev(1, "next_step_waiting", {
        unmet: ['pr_merged("spec/204-*", "main") = false'],
        needs: ["the pull request from spec/204-… merged into main"],
      }),
    );
    expect(worded.primary).toBe("the pull request from spec/204-… merged into main");
    expect(worded.long).toEqual({
      title: "Waiting for",
      body: 'the pull request from spec/204-… merged into main\n\nRules checked:\npr_merged("spec/204-*", "main") = false',
    });
    const older = factsFor(
      ev(1, "next_step_waiting", { unmet: ['pr_merged("spec/204-*", "main") = false'] }),
    );
    expect(older.primary).toBe('pr_merged("spec/204-*", "main") = false');
  });

  it("words what a refused retry still needs, falling back to the rules on an older row", () => {
    const unmet = ["ticket_has_assignee() = false"];
    expect(
      factsFor(
        ev(1, "step_retry_refused", {
          reason: "preconditions_unmet",
          unmet,
          needs: ["an assignee on the issue"],
        }),
      ).primary,
    ).toBe("the step can't start yet: an assignee on the issue");
    expect(
      factsFor(ev(1, "step_retry_refused", { reason: "preconditions_unmet", unmet })).primary,
    ).toBe("the step can't start yet: ticket_has_assignee() = false");
  });

  it("names an issue opened for a client by its number alone, recovered or not", () => {
    const facts = factsFor(ev(1, "issue_created", { number: 52, recovered: true }));
    expect(facts.primary).toBe("#52");
  });

  it("says why GitHub refused a gate's merge, as something still waiting", () => {
    const reason = 'Required status check "verify" is expected.';
    const facts = factsFor(
      ev(1, "gate_merge_refused", {
        pr_number: 248,
        approved_by: "teammate-3",
        status: 405,
        reason,
      }),
    );
    expect(facts.label).toBe("Merge refused");
    expect(facts.primary).toBe(reason);
    expect(facts.long?.body).toBe(reason);
    expect(facts.tone).toBe("attention");
    expect(facts.significant).toBe(true);
    expect(linkFor(ev(2, "gate_merge_refused", { pr_number: 248 }), REPO)?.label).toBe("PR #248");
  });

  it("says a closed pull request held the gate, as something still waiting", () => {
    const facts = factsFor(ev(1, "gate_pr_closed", { pr_number: 248, head_sha: "abc" }));
    expect(facts.label).toBe("Pull request closed");
    expect(facts.primary).toBe("closed without merging; the gate holds");
    expect(facts.tone).toBe("attention");
    expect(linkFor(ev(2, "gate_pr_closed", { pr_number: 248 }), REPO)?.label).toBe("PR #248");
  });

  it("counts an un-block's granted attempt down to the backoff it still owes", () => {
    // The wait after an un-block is the SAME backoff clock an ordinary retry waits out
    // (`_agent_step` gates on `_retry_ready` either way), so it reads the same way on the page.
    const facts = factsFor(
      ev(1, "unblocked", {
        step: "plan",
        attempt: 2,
        next_attempt: 3,
        due_at: "2026-08-25T09:34:18+00:00",
      }),
    );

    expect(facts.dueAt).toBe("2026-08-25T09:34:18+00:00");
    expect(facts.tone).toBe("attention");
  });

  it("shows no countdown on an un-block whose wait is not a clock", () => {
    // A gate reopens on the next tick and an unmet precondition is released by a human acting
    // elsewhere; `_grant_attempt` sends a null `due_at` for both, and a timer that runs out on
    // nothing happening would be worse than no timer.
    expect(
      factsFor(ev(1, "unblocked", { step: "spec_gate", next_attempt: 2 })).dueAt,
    ).toBeUndefined();
    expect(
      factsFor(ev(2, "unblocked", { step: "plan", next_attempt: 3, due_at: null })).dueAt,
    ).toBeUndefined();
  });

  it("reads the reason a gate rejection actually carries", () => {
    // `worker/gates.py::_reject` emits `pr_number` and `reason`.
    const facts = factsFor(
      ev(1, "gate_rejected", {
        step: "spec_gate",
        attempt: 1,
        pr_number: 7,
        reason: "changes requested by teammate-3",
      }),
    );

    expect(facts.primary).toBe("changes requested by teammate-3");
    expect(facts.tone).toBe("error");
  });

  it("renders the quarantine event's error as its primary fact", () => {
    const facts = factsFor(
      ev(1, "ticket_quarantined", {
        error: "GitHub API error: 403 archived repository",
        failures: 5,
      }),
    );
    expect(facts.label).toBe("Stopped after errors");
    expect(facts.primary).toBe("GitHub API error: 403 archived repository");
    expect(facts.tone).toBe("error");
    expect(facts.significant).toBe(true);
  });

  it("folds a long quarantine error into the modal, like a failure reason", () => {
    const long = "e".repeat(200);
    const facts = factsFor(ev(1, "ticket_quarantined", { error: long, failures: 5 }));
    expect(facts.long?.body).toBe(long);
    expect(facts.primary?.length).toBeLessThanOrEqual(120);
    expect(facts.primary?.endsWith("…")).toBe(true);
  });

  it("renders the requeue as the quarantine's answer, in the neutral ink", () => {
    // Neutral, not error: the quarantine row above it is the fault, and this is the recovery.
    // Significant, because a log that shows the giving-up but not the putting-back reads as a
    // ticket nobody ever came back to.
    const facts = factsFor(ev(1, "ticket_requeued", { requested_via: "dashboard", failures: 5 }));
    expect(facts.label).toBe("Requeued");
    expect(facts.primary).toBe("from the dashboard, after 5 failed checks");
    expect(facts.tone).toBe("neutral");
    expect(facts.significant).toBe(true);
  });

  it("renders an event type this build has never heard of", () => {
    // `event.type` is TEXT with no CHECK, so the log can record a type this build does not know.
    const facts = factsFor(ev(1, "budget_exceeded", { step: "build", attempt: 2, limit: "5.00" }));
    expect(facts.label).toBe("budget exceeded");
    expect(facts.primary).toBe("limit: 5.00");
  });

  it("hands the row the due time the worker recorded, not a backoff nobody emits", () => {
    // `worker/failures.py` writes `due_at`; the row once read `backoff`, which it never emits.
    const facts = factsFor(
      ev(1, "retry_scheduled", {
        step: "plan",
        attempt: 1,
        next_attempt: 2,
        max_attempts: 2,
        due_at: "2026-08-25T09:34:18+00:00",
      }),
    );

    expect(facts.dueAt).toBe("2026-08-25T09:34:18+00:00");
    expect(facts.primary).toBeNull(); // the countdown owns the column
    expect(facts.tone).toBe("attention");
  });

  it("declines a due time it cannot read, rather than counting down to nothing", () => {
    expect(factsFor(ev(1, "retry_scheduled", { due_at: "in a bit" })).dueAt).toBeUndefined();
    expect(factsFor(ev(2, "retry_scheduled", {})).dueAt).toBeUndefined();
  });

  it("never offers a positional key as the primary fact", () => {
    const facts = factsFor(ev(1, "mystery_event", { step: "build", attempt: 2 }));
    expect(facts.primary).toBeNull();
  });
});

describe("factsFor on a step entry", () => {
  it("names where the ticket came from", () => {
    const facts = factsFor(ev(1, "step_entered", { step: "spec_gate", from_step: "plan" }));
    expect(facts.label).toBe("Entered step");
    expect(facts.primary).toBe("from plan");
  });

  it("says a gate rejection sent the work back, which a step name alone cannot", () => {
    const facts = factsFor(
      ev(1, "step_entered", { step: "plan", from_step: "spec_gate", reason: "reject_rerun" }),
    );
    expect(facts.primary).toBe("rework, sent back from spec_gate");
  });

  it("is bracketing, not a reason anyone opened the page", () => {
    expect(factsFor(ev(1, "step_entered", { step: "plan" })).significant).toBe(false);
  });

  it("files under the attempt it predicts, so the run that follows joins the same block", () => {
    const entries = [
      ev(1, "step_entered", { step: "plan", attempt: 2, kind: "agent" }),
      ev(2, "step_started", { step: "plan", attempt: 2, kind: "agent" }),
    ];
    expect(segmentsFrom(entries, []).map((s) => s.key)).toEqual(["plan#2"]);
  });

  it("files a terminal entry with ticket_done, which carries no attempt either", () => {
    const entries = [
      ev(1, "step_entered", { step: "done", attempt: null, kind: "terminal" }),
      ev(2, "ticket_done", { step: "done" }),
    ];
    expect(segmentsFrom(entries, []).map((s) => s.key)).toEqual(["done"]);
  });
});

describe("snippet", () => {
  it("collapses a paragraph to one line", () => {
    expect(snippet("one\ntwo   three\n\nsecond para")).toBe("one two three");
  });

  it("leaves a short string alone", () => {
    expect(snippet("short")).toBe("short");
  });
});

describe("attemptKeyForNode", () => {
  // Three attempts at `plan` with a gate in between, in log order — what `segmentsFrom` returns.
  const segments = segmentsFrom(
    [
      ev(1, "ticket_admitted", { step: "plan" }),
      ev(2, "step_started", { step: "plan", attempt: 1 }),
      ev(3, "step_started", { step: "spec_gate", attempt: 1 }),
      ev(4, "step_started", { step: "plan", attempt: 2 }),
      ev(5, "step_started", { step: "plan", attempt: 3 }),
    ],
    [],
  );

  it("opens a node the reader is not in at its most recent attempt", () => {
    expect(attemptKeyForNode(segments, "plan", null)).toBe("plan#3");
    expect(attemptKeyForNode(segments, "plan", "spec_gate#1")).toBe("plan#3");
  });

  it("ignores which end of the log the reader is reading from", () => {
    // The newest-first toggle reverses the blocks; "most recent" must not reverse with it.
    expect(attemptKeyForNode(sortSegments(segments, "newest"), "plan", null)).toBe("plan#3");
  });

  it("steps back through the earlier attempts of the node it is already in, and wraps", () => {
    expect(attemptKeyForNode(segments, "plan", "plan#3")).toBe("plan#2");
    expect(attemptKeyForNode(segments, "plan", "plan#2")).toBe("plan#1");
    expect(attemptKeyForNode(segments, "plan", "plan#1")).toBe("plan#3");
  });

  it("stays put on a step that has only run once", () => {
    expect(attemptKeyForNode(segments, "spec_gate", "spec_gate#1")).toBe("spec_gate#1");
  });

  it("finds the admission block, which is not an attempt at anything", () => {
    expect(attemptKeyForNode(segments, ENTRY_NODE_ID, null)).toBe(ENTRY_NODE_ID);
  });

  it("has nothing to select for a node that has never run", () => {
    expect(attemptKeyForNode(segments, "implement", null)).toBeNull();
  });
});

describe("sortSegments", () => {
  const segments = segmentsFrom(
    [
      ev(1, "step_started", { step: "plan", attempt: 1 }),
      ev(2, "postcondition_failed", { step: "plan", attempt: 1 }),
      ev(3, "step_started", { step: "plan", attempt: 2 }),
    ],
    [run("plan", 1, "failed"), run("plan", 2, "running")],
  );

  it("leaves the log order alone when that is what was asked for", () => {
    expect(sortSegments(segments, "oldest").map((s) => s.key)).toEqual(["plan#1", "plan#2"]);
    expect(sortSegments(segments, "oldest")[0]?.entries.map((e) => e.id)).toEqual([1, 2]);
  });

  it("turns the blocks AND their events around for newest-first", () => {
    const newest = sortSegments(segments, "newest");
    expect(newest.map((s) => s.key)).toEqual(["plan#2", "plan#1"]);
    // A block reading oldest-first inside a newest-first page tells the reader nothing.
    expect(newest[1]?.entries.map((e) => e.id)).toEqual([2, 1]);
  });

  it("brings the event-less runs to the top, where the newest thing belongs", () => {
    // `segmentsFrom` pins a run whose events have not arrived yet to the newest end.
    const withOrphan = segmentsFrom(
      [ev(1, "step_started", { step: "plan", attempt: 1 })],
      [run("plan", 1, "succeeded"), run("build", 1, "running")],
    );
    expect(sortSegments(withOrphan, "newest").map((s) => s.key)).toEqual(["build#1", "plan#1"]);
  });

  it("does not disturb the array it was handed", () => {
    const before = segments.map((s) => s.key);
    sortSegments(segments, "newest");
    expect(segments.map((s) => s.key)).toEqual(before);
    expect(segments[0]?.entries.map((e) => e.id)).toEqual([1, 2]);
  });
});

describe("totalsFrom", () => {
  it("sums tokens, cost and time across every attempt", () => {
    const totals = totalsFrom([
      run("plan", 1, "failed", {
        input_tokens: 41880,
        output_tokens: 9204,
        cost_usd: "0.9441",
        started_at: "2026-08-25T09:05:00Z",
        ended_at: "2026-08-25T09:24:00Z",
      }),
      run("plan", 2, "succeeded", {
        input_tokens: 12000,
        output_tokens: 3000,
        cost_usd: "0.2559",
        started_at: "2026-08-25T09:34:00Z",
        ended_at: "2026-08-25T09:40:00Z",
      }),
    ]);

    expect(totals.inputTokens).toBe(53880);
    expect(totals.outputTokens).toBe(12204);
    // The whole column, at the scale the API sends a single run's figure in.
    expect(totals.costUsd).toBe("1.2000");
    expect(totals).toMatchObject({ priced: true, estimated: false });
    // 19m + 6m — the 10-minute gap between the attempts is not time the ticket spent working.
    expect(totals.seconds).toBe(25 * 60);
  });

  it("counts a run that is still going up to now", () => {
    vi.useFakeTimers().setSystemTime(new Date("2026-08-25T09:14:00Z"));
    const totals = totalsFrom([run("plan", 1, "running", { started_at: "2026-08-25T09:05:00Z" })]);
    expect(totals.seconds).toBe(9 * 60);
  });

  it("treats an unreported figure as nothing spent, not as a gap in the sum", () => {
    const totals = totalsFrom([run("spec_gate", 1, "waiting", { kind: "human_gate" })]);
    expect(totals).toMatchObject({ runs: 1, inputTokens: 0, outputTokens: 0, costUsd: "0.0000" });
    expect(totals.seconds).toBe(0);
    // ...but says no run was priced, so the header can draw a dash instead of a free run.
    expect(totals.priced).toBe(false);
  });

  it("calls the sum an estimate when any part of it is one", () => {
    const totals = totalsFrom([
      run("plan", 1, "succeeded", { cost_usd: "0.9441", cost_source: "reported" }),
      run("build", 1, "succeeded", { cost_usd: "0.2559", cost_source: "estimated" }),
    ]);
    expect(totals).toMatchObject({ costUsd: "1.2000", priced: true, estimated: true });
  });

  it("reports no runs, so the header can stay empty rather than showing zeroes", () => {
    expect(totalsFrom([]).runs).toBe(0);
  });
});

describe("linkFor", () => {
  it("links the PR a gate decided on", () => {
    expect(linkFor(ev(1, "gate_approved", { pr_number: 248 }), REPO)).toEqual({
      label: "PR #248",
      href: `https://github.com/${REPO}/pull/248`,
    });
  });

  it("links a GitLab ticket to the merge request, issue, branch, and commit", () => {
    const gitlab = forgeHost({ kind: "gitlab" }, {});
    expect(linkFor(ev(1, "gate_approved", { pr_number: 248 }), REPO, gitlab)).toEqual({
      label: "MR #248",
      href: `https://gitlab.com/${REPO}/-/merge_requests/248`,
    });
    expect(linkFor(ev(1, "ticket_admitted", { external_key: "204" }), REPO, gitlab)?.href).toBe(
      `https://gitlab.com/${REPO}/-/issues/204`,
    );
  });

  it("links the issue admission recorded", () => {
    expect(linkFor(ev(1, "ticket_admitted", { external_key: "204" }), REPO)).toEqual({
      label: "issue #204",
      href: `https://github.com/${REPO}/issues/204`,
    });
  });

  it("keeps the slashes in a branch name, which are path separators in the tree URL", () => {
    const link = linkFor(ev(1, "agent_spawned", { branch: "spec/204-remit-to" }), REPO);
    expect(link).toEqual({
      label: "spec/204-remit-to",
      href: `https://github.com/${REPO}/tree/spec/204-remit-to`,
    });
  });

  it("links the branch a completed run actually delivered", () => {
    // The answer to the spawn row's template: `worker/acceptance.py::_delivered_branch` reads it
    // off the artifacts the run reported, so the block carries one link that resolves.
    expect(
      linkFor(ev(1, "agent_completed", { branch: "spec/4-motion-core-scaffold" }), REPO),
    ).toEqual({
      label: "spec/4-motion-core-scaffold",
      href: `https://github.com/${REPO}/tree/spec/4-motion-core-scaffold`,
    });
  });

  it("says nothing when a completed run named no single branch", () => {
    expect(linkFor(ev(1, "agent_completed", { branch: null }), REPO)).toBeNull();
  });

  it("names an unresolved branch template without linking it", () => {
    // `{slug}` is the agent's to choose, so the branch the spawn RECORDS is a directive, not an
    // observation (`worker/agent_steps.py`). A tree URL built from it points at no ref.
    expect(linkFor(ev(1, "agent_spawned", { branch: "impl/4-{slug}" }), REPO)).toEqual({
      label: "impl/4-{slug}",
      href: null,
    });
  });

  it("escapes the rest of an agent-chosen branch name", () => {
    // `{slug}` is whatever the agent pushed; it reaches the URL as data, never as syntax.
    expect(linkFor(ev(1, "agent_spawned", { branch: "spec/204 a?b" }), REPO)?.href).toBe(
      `https://github.com/${REPO}/tree/spec/204%20a%3Fb`,
    );
  });

  it("prefers the PR over the commit the same event names", () => {
    const link = linkFor(
      ev(1, "gate_approved", { pr_number: 248, merged_sha: "0123456789ab" }),
      REPO,
    );
    expect(link?.label).toBe("PR #248");
  });

  it("links a merge commit in its short form", () => {
    expect(linkFor(ev(1, "gate_approved", { merged_sha: "0123456789abcdef" }), REPO)).toEqual({
      label: "0123456",
      href: `https://github.com/${REPO}/commit/0123456789abcdef`,
    });
  });

  it("ignores a sha-shaped key that is not a sha", () => {
    expect(linkFor(ev(1, "action_executed", { contract_sha: "pending" }), REPO)).toBeNull();
  });

  it("reads the generic keys only on the dry-run event that defines them", () => {
    // `number` and `name` are the projector call's own argument names; on any other type they
    // could mean anything, and a link to the wrong thing is worse than no link.
    const suppressed = ev(1, "dry_run_write_suppressed", {
      verb: "close_pull_request",
      number: 7,
    });
    expect(linkFor(suppressed, REPO)?.label).toBe("PR #7");
    expect(linkFor(ev(2, "budget_exceeded", { number: 7 }), REPO)).toBeNull();
  });

  it("has nothing to link when the event names no GitHub thing", () => {
    expect(linkFor(ev(1, "step_completed", { step: "plan", attempt: 1 }), REPO)).toBeNull();
  });

  it("links nothing before the project query has said which repo this is", () => {
    expect(linkFor(ev(1, "gate_approved", { pr_number: 248 }), undefined)).toBeNull();
  });
});

describe("heldAt", () => {
  it("names the attempt a ticket waiting on a human is stopped at", () => {
    // `step#attempt`, the key `segmentsFrom` gives that block: the ticket is stopped at ONE
    // attempt of the step, and the earlier attempts of the same step are finished history.
    const held = heldAt({ status: "waiting_gate", current_step: "spec_gate" }, [
      run("plan", 1, "succeeded"),
      run("spec_gate", 1, "rejected"),
      run("spec_gate", 2, "waiting"),
    ]);
    expect(held).toEqual({ key: "spec_gate#2", condition: "waiting_gate" });
  });

  it("carries the same condition the header pill and the graph node draw", () => {
    // The link that points at the node has to be told by the same thing the node is, or the two
    // disagree on screen about both the colour and the beat.
    const held = heldAt({ status: "blocked", current_step: "plan" }, [run("plan", 2, "failed")]);
    expect(held).toEqual({ key: "plan#2", condition: "blocked" });
  });

  it("tells a run somebody stopped from one that gave up", () => {
    // Both park the ticket with `status = blocked`; only the run says which happened.
    const held = heldAt({ status: "blocked", current_step: "plan" }, [
      run("plan", 1, "failed"),
      run("plan", 2, "interrupted"),
    ]);
    expect(held).toEqual({ key: "plan#2", condition: "interrupted" });
  });

  it("keeps a rejected gate apart from a failure, exactly as the graph paints it", () => {
    const held = heldAt({ status: "blocked", current_step: "spec_gate" }, [
      run("spec_gate", 1, "rejected"),
    ]);
    expect(held).toEqual({ key: "spec_gate#1", condition: "rejected" });
  });

  it("is nothing while the ticket is still running", () => {
    expect(heldAt({ status: "running", current_step: "plan" }, [run("plan", 1, "running")])).toBe(
      null,
    );
    expect(heldAt({ status: "done", current_step: "done" }, [])).toBe(null);
  });

  it("is nothing until the step it is held at has a run to point at", () => {
    // No block on screen yet, so there is nothing to mark.
    expect(
      heldAt({ status: "waiting_gate", current_step: "spec_gate" }, [run("plan", 1, "succeeded")]),
    ).toBe(null);
  });
});

describe("linkPulse", () => {
  const held = { key: "spec_gate#2", condition: "waiting_gate" } as const;

  it("beats on the row that says why the ticket is stopped", () => {
    expect(linkPulse(held, true)).toBe("blink");
  });

  it("marks a failed hold as an error instead of a beat", () => {
    expect(linkPulse({ key: "plan#2", condition: "blocked" }, true)).toBe("error");
  });

  it("leaves a ticket somebody parked on the ordinary beat", () => {
    // An interrupt is a deliberate stop, not a failure, so it keeps the ordinary blink.
    expect(linkPulse({ key: "plan#2", condition: "interrupted" }, true)).toBe("blink");
  });

  it("leaves the bookkeeping rows of the held attempt still", () => {
    // `agent_spawned` carries a branch link and sits in the same block as the escalation. Only
    // the row a reader is being sent to should move.
    expect(linkPulse(held, false)).toBe("none");
  });

  it("leaves every block the ticket is not stopped at still", () => {
    // The panel passes null for any block that is not the held one — an earlier attempt of the
    // same step included, since it is finished and asks nothing of anyone.
    expect(linkPulse(null, true)).toBe("none");
  });
});

describe("factsFor on a human interrupt", () => {
  // These three events shipped with decision 26 and never reached `factsFor`, so they fell to the
  // unknown-type default: grey, neutral, and labelled `interrupt requested`. That made the two
  // rows explaining why a ticket is parked read as bookkeeping, in a block where everything else
  // genuinely is.
  it("names each of the three rather than spelling out the enum member", () => {
    expect(factsFor(ev(1, "interrupt_requested", {})).label).toBe("Interrupt requested");
    expect(factsFor(ev(2, "agent_interrupted", {})).label).toBe("Interrupted");
    expect(factsFor(ev(3, "agent_resumed", {})).label).toBe("Resumed");
  });

  it("draws them in the amber a parked ticket already reads as", () => {
    // `attention` is the ink `status.ts` gives the `interrupted` condition overhead and the
    // `interrupted` tag on the attempt.
    for (const type of ["interrupt_requested", "agent_interrupted", "agent_resumed"]) {
      const facts = factsFor(ev(1, type, { step: "plan", attempt: 2 }));
      expect(facts.tone).toBe("attention");
      expect(facts.significant).toBe(true);
    }
  });

  it("says an overtaken request was not applied, and why, rather than looking ignored", () => {
    const facts = factsFor(ev(1, "interrupt_overtaken", { step: "build", attempt: 1 }));
    expect(facts.label).toBe("Interrupt not applied");
    expect(facts.primary).toBe("the run had already finished");
    expect(facts.tone).toBe("attention");
    expect(facts.significant).toBe(true);
  });

  it("says which surface asked, never who — the API cannot know", () => {
    // One shared token, not a user (decision 12), so the event carries `requested_via` and the
    // orchestrator's own ticket comment says "from the dashboard" rather than naming anybody.
    const facts = factsFor(ev(1, "interrupt_requested", { requested_via: "dashboard" }));
    expect(facts.primary).toBe("from the dashboard");
  });

  it("says a request a person approved in the assistant came from the assistant", () => {
    // `events/types.py`'s REQUESTED_VIA_WORDS, which the ticket comment uses too.
    expect(factsFor(ev(1, "interrupt_requested", { requested_via: "chat" })).primary).toBe(
      "from the assistant",
    );
    expect(factsFor(ev(2, "ticket_exited", { via: "chat" })).primary).toBe("from the assistant");
  });

  it("tells the reader how to release the ticket, but only while it is still parked", () => {
    const held = ev(1, "agent_interrupted", {
      step: "plan",
      attempt: 2,
      run_id: "204-plan-agent-1",
    });
    expect(factsFor(held, true).primary).toBe(
      "To resume, remove the factorial:interrupted label from the issue",
    );
    // The same event read as history is a record that a run was stopped, not a second instruction
    // — the rule `gate_waiting` already follows.
    expect(factsFor(held).primary).toBeNull();
  });

  it("tells the reader to remove factorial:waiting to release an idle park, only while parked", () => {
    const idle = ev(1, "agent_parked", { step: "plan", attempt: 2, reason: "idle" });
    expect(factsFor(idle).label).toBe("Paused");
    expect(factsFor(idle, true).primary).toContain("factorial:waiting");
    expect(factsFor(idle, true).primary).not.toContain("factorial:interrupted");
    expect(factsFor(idle).primary).toBeNull();
    expect(factsFor(idle, true).tone).toBe("attention");
  });

  it("names a session ended from its terminal, and how to resume it while parked", () => {
    const exited = ev(1, "agent_parked", { reason: "exited" });
    const now = factsFor(exited, currentRows([exited])(exited));
    expect(now.primary).toBe(
      "Ended from its terminal. To resume, press Resume or remove the factorial:interrupted label from the issue",
    );
    expect(now.tone).toBe("attention");
    expect(now.significant).toBe(true);
    const history = factsFor(exited);
    expect(history.primary).toBe("Ended from its terminal");
    expect(history.tone).toBe("neutral");
  });

  it("asks nothing of a rollover park, which resumes on the same tick", () => {
    const parked = ev(1, "agent_parked", { reason: "rollover" });
    const entries = [parked, ev(2, "agent_resumed", { via: "rollover" })];
    const facts = factsFor(parked, currentRows(entries)(parked));
    expect(facts.primary).toBeNull();
    expect(facts.tone).toBe("neutral");
    expect(facts.significant).toBe(false);
  });

  it("tells the reader to remove factorial:interrupted when a rollover's resume was refused", () => {
    const parked = ev(1, "agent_parked", { reason: "rollover" });
    const facts = factsFor(parked, currentRows([parked])(parked));
    expect(facts.primary).toContain("factorial:interrupted");
    expect(facts.tone).toBe("attention");
  });

  it("says how to resume a session at its ceiling while parked, and why it stopped after", () => {
    const parked = ev(3, "agent_parked", { reason: "ceiling", live_seconds: 14400 });
    // Newest first, as the panel shows it by default, after the rollovers a 4-hour session had.
    const newestFirst = [
      parked,
      ev(2, "agent_resumed", { via: "rollover" }),
      ev(1, "agent_parked", { reason: "rollover" }),
    ];
    expect(factsFor(parked, currentRows(newestFirst)(parked)).primary).toBe(
      "Paused at the session time limit (4h 0m). Press Resume to continue",
    );
    const history = factsFor(parked);
    expect(history.primary).toBe("Paused at the session time limit (4h 0m)");
    expect(history.significant).toBe(true);
  });

  it("says a stop a park overtook found the run parked, not finished", () => {
    const facts = factsFor(ev(1, "interrupt_overtaken", { reason: "parked" }));
    expect(facts.primary).toBe("the run was already paused");
  });

  it("never prints the runtime id, which names nothing a reader can act on", () => {
    const facts = factsFor(ev(1, "agent_interrupted", { run_id: "204-plan-agent-71bc55" }), true);
    expect(facts.primary).not.toContain("204-plan-agent-71bc55");
  });

  it("says who steered a resumed run, by reference", () => {
    const facts = factsFor(
      ev(1, "agent_resumed", { comment_count: 2, steered_by: ["teammate-3", "operator"] }),
    );
    expect(facts.primary).toBe("2 comments from teammate-3, operator");
  });

  it("says so plainly when a resume was left no instructions", () => {
    const facts = factsFor(ev(1, "agent_resumed", { comment_count: 0, steered_by: [] }));
    expect(facts.primary).toBe("no new instructions");
  });

  it("counts one comment in the singular", () => {
    const facts = factsFor(
      ev(1, "agent_resumed", { comment_count: 1, steered_by: ["teammate-3"] }),
    );
    expect(facts.primary).toBe("1 comment from teammate-3");
  });
});

describe("exit and re-entry rows (decision 29)", () => {
  it("names each event plainly", () => {
    expect(factsFor(ev(1, "exit_requested", {})).label).toBe("Exit requested");
    expect(factsFor(ev(2, "ticket_exited", {})).label).toBe("Exited");
    expect(factsFor(ev(3, "reentry_requested", {})).label).toBe("Re-entry requested");
    expect(factsFor(ev(4, "ticket_reentered", {})).label).toBe("Re-entered");
    expect(factsFor(ev(5, "reentry_refused", {})).label).toBe("Re-entry refused");
  });

  it("says a Close and exit asked for the issue closed too", () => {
    expect(
      factsFor(ev(1, "exit_requested", { requested_via: "dashboard", close_issue: true })).primary,
    ).toBe("from the dashboard, closing the issue");
    expect(factsFor(ev(2, "exit_requested", { requested_via: "dashboard" })).primary).toBe(
      "from the dashboard",
    );
  });

  it("says which way in fired, in the words the ticket comment uses", () => {
    expect(factsFor(ev(1, "ticket_exited", { via: "issue_closed" })).primary).toBe(
      "the issue was closed",
    );
    expect(factsFor(ev(1, "ticket_exited", { via: "label" })).primary).toBe(
      "the factorial:exit label was added",
    );
    expect(factsFor(ev(1, "ticket_exited", { via: "dashboard" })).primary).toBe(
      "from the dashboard",
    );
  });

  it("says why it left, in a held block as much as in history", () => {
    // `current` comes from `heldAt`, which is null unless the ticket is WAITING_GATE or BLOCKED,
    // so it is never true for an EXITED one. The "remove the label" line could therefore only
    // ever reach a LATER block that happens to hold a historical exit row — telling its reader
    // to remove a label the ticket does not carry. The header's re-enter button is where that
    // affordance belongs.
    const away = ev(1, "ticket_exited", { via: "label", step: "plan", attempt: 1 });
    expect(factsFor(away, true).primary).toBe("the factorial:exit label was added");
    expect(factsFor(away).primary).toBe("the factorial:exit label was added");
  });

  it("draws exit in the neutral ink and a refusal in amber", () => {
    expect(factsFor(ev(1, "ticket_exited", {})).tone).toBe("neutral");
    expect(factsFor(ev(1, "ticket_reentered", {})).tone).toBe("neutral");
    expect(factsFor(ev(1, "reentry_refused", { reason: "issue_closed" })).tone).toBe("attention");
    expect(factsFor(ev(1, "reentry_refused", { reason: "issue_closed" })).primary).toBe(
      "the issue is closed; reopen it first",
    );
    expect(factsFor(ev(1, "reentry_refused", { reason: "no_retry_policy" })).primary).toBe(
      "this step's process has no retry rule",
    );
  });

  it("counts a re-entry's granted attempt down the way an un-block's is counted", () => {
    // Same release, same backoff clock — so the row reads the same, and null wherever the wait
    // is not a clock at all.
    expect(factsFor(ev(1, "ticket_reentered", { due_at: "2026-08-25T09:34:18+00:00" })).dueAt).toBe(
      "2026-08-25T09:34:18+00:00",
    );
    expect(factsFor(ev(2, "ticket_reentered", { due_at: null })).dueAt).toBeUndefined();
  });

  it("says when a re-entry continued the interrupted session", () => {
    expect(factsFor(ev(1, "ticket_reentered", { resumed: true })).primary).toBe(
      "continued the interrupted session",
    );
    expect(factsFor(ev(1, "ticket_reentered", { resumed: false })).primary).toBeNull();
  });
});

describe("factsFor on a gate", () => {
  const waiting = ev(1, "gate_waiting", { step: "spec_gate", attempt: 2, pr_number: 248 });

  it("says what the reader has to do while the ticket is still waiting on them", () => {
    expect(factsFor(waiting, true).primary).toBe(
      "Review the pull request, then approve it or request changes",
    );
  });

  it("asks nothing of a gate that has already been decided", () => {
    // The same event, read as history: the label and the PR link say everything, and an
    // instruction on a finished attempt is an instruction to do something twice.
    expect(factsFor(waiting).primary).toBeNull();
  });

  it("never repeats the PR the row's own link already names", () => {
    expect(factsFor(waiting, true).primary).not.toContain("248");
  });
});

describe("statusDot", () => {
  it("gives an attempt the role its own lozenge reads as", () => {
    // A warning `interrupted` tag must not sit above a neutral dot.
    expect(statusDot("interrupted")).toBe("bg-chart-warning");
    expect(statusDot("waiting")).toBe("bg-chart-warning");
    expect(statusDot("rejected")).toBe("bg-chart-discovery");
    expect(statusDot("failed")).toBe("bg-chart-danger");
    expect(statusDot("timeout")).toBe("bg-chart-danger");
    expect(statusDot("succeeded")).toBe("bg-chart-success");
    expect(statusDot("approved")).toBe("bg-chart-success");
    expect(statusDot("running")).toBe("bg-chart-information");
    expect(statusDot("cancelled")).toBe("bg-chart-neutral");
  });

  it("leaves the admission block unmarked — it is not an attempt at anything", () => {
    expect(statusDot("info")).toBe("bg-chart-neutral");
  });

  it("marks a forced run in the warning role, never the success one", () => {
    const [segment] = segmentsFrom(
      [ev(1, "step_started", { step: "plan", attempt: 1 })],
      [run("plan", 1, "succeeded", { forced_by: "op@example.com" })],
    );
    expect(segment?.status).toBe("forced");
    expect(statusDot(segment?.status ?? "info")).toBe("bg-chart-warning");
  });
});

describe("factsFor, previews", () => {
  it("shows a preview's host, or its withdrawal, from the URL list", () => {
    const published = factsFor(
      ev(1, "preview_url_updated", {
        urls: [{ service: "web", url: "https://3000-sbx.e2b.app" }],
      }),
    );
    const withdrawn = factsFor(ev(2, "preview_url_updated", { urls: [] }));
    const legacy = factsFor(ev(3, "preview_url_updated", { url: "https://preview.example/1" }));
    expect(published.primary).toBe("3000-sbx.e2b.app");
    expect(withdrawn.label).toBe("Preview withdrawn");
    expect(legacy.primary).toBe("preview.example");
  });

  it("shows why a preview failed, folded, as text", () => {
    const facts = factsFor(ev(1, "preview_failed", { reason: "setup failed:\nnpm ERR! boom" }));
    expect(facts.label).toBe("Preview failed");
    expect(facts.tone).toBe("attention");
    expect(facts.primary).toBe("setup failed:");
    expect(facts.long?.body).toContain("npm ERR! boom");
  });

  it("labels a page observation by what was found", () => {
    expect(
      factsFor(ev(1, "preview_page_observed", { status: "published", commit: "c".repeat(40) })),
    ).toMatchObject({
      label: "Page published",
      primary: "ccccccc",
      tone: "neutral",
      significant: false,
    });
    expect(factsFor(ev(2, "preview_page_observed", { status: "missing" })).label).toBe(
      "No page published",
    );
    expect(
      factsFor(
        ev(3, "preview_page_observed", { status: "lookup_failed", reason: "GitHub is down" }),
      ),
    ).toMatchObject({ label: "Page lookup failed", primary: "GitHub is down", tone: "attention" });
  });

  it("words a run's recorded outputs by their count, as an insignificant row", () => {
    const file = { path: "a.md", blob: "b", size: 1, changed: true };
    expect(
      factsFor(ev(1, "run_outputs_observed", { status: "observed", files: [file, file, file] })),
    ).toMatchObject({
      label: "Outputs recorded",
      primary: "3 files",
      tone: "neutral",
      significant: false,
    });
    expect(
      factsFor(ev(2, "run_outputs_observed", { status: "observed", files: [file] })).primary,
    ).toBe("1 file");
    expect(
      factsFor(
        ev(3, "run_outputs_observed", { status: "lookup_failed", reason: "GitHub is down" }),
      ),
    ).toMatchObject({
      label: "Couldn't read files",
      primary: "GitHub is down",
      tone: "attention",
    });
  });

  it("names who kept a preview running", () => {
    const facts = factsFor(ev(1, "preview_kept", { requested_by: "a@x.dev" }));
    expect(facts.label).toBe("Preview kept running");
    expect(facts.primary).toBe("by a@x.dev");
  });

  it("names who started or stopped a preview", () => {
    expect(factsFor(ev(1, "preview_requested", { requested_by: "a@x.dev" })).primary).toBe(
      "by a@x.dev",
    );
    expect(factsFor(ev(2, "preview_stopped", { reason: "stopped by a@x.dev" })).primary).toBe(
      "stopped by a@x.dev",
    );
    expect(factsFor(ev(3, "preview_started", {})).label).toBe("Preview started");
  });

  it("words the refusal codes the loop records, and shows an older event's sentence as it is", () => {
    expect(factsFor(ev(1, "session_resume_refused", { reason: "no_attachments" })).primary).toBe(
      "this runtime can't pass on the images in the comments",
    );
    expect(
      factsFor(ev(2, "session_resume_refused", { reason: "could not resolve this agent: x" }))
        .primary,
    ).toBe("could not resolve this agent: x");
    expect(
      factsFor(
        ev(3, "step_changes_refused", {
          reason: "not_sent_back",
          step: "spec_gate",
          reviewed_step: "plan",
        }),
      ).primary,
    ).toBe("spec_gate can't send work back to plan");
  });

  it("words why an open session closed, with the parameters its event carries", () => {
    expect(
      factsFor(ev(1, "inspect_parked", { reason: "limit", limit_seconds: 3300 })).primary,
    ).toBe("it reached its time limit of 55 minutes");
    expect(
      factsFor(ev(4, "inspect_parked", { reason: "unwatched", after_seconds: 90 })).primary,
    ).toBe("nobody watched it for 90 seconds");
    expect(
      factsFor(ev(5, "inspect_parked", { reason: "unwatched", after_seconds: 60 })).primary,
    ).toBe("nobody watched it for 1 minute");
    expect(
      factsFor(ev(2, "inspect_parked", { reason: "closed", closed_by: "a@x.dev" })).primary,
    ).toBe("a@x.dev closed it");
    expect(factsFor(ev(3, "inspect_parked", { reason: "unreachable" })).primary).toBe(
      "the sandbox couldn't be reached",
    );
  });

  it("words the codes a preview stop records, and keeps what a failed renewal said", () => {
    expect(factsFor(ev(1, "preview_stopped", { reason: "continued" })).primary).toBe(
      "A later attempt continues in this workspace.",
    );
    const failed = factsFor(ev(2, "preview_stopped", { reason: "renewal\nthe sandbox is gone" }));
    expect(failed.primary).toBe("Stopped while its sandbox was renewed.");
    expect(failed.long?.body).toBe("the sandbox is gone");
  });
});

describe("gate decision facts", () => {
  it("names the role a reviewer counted by", () => {
    const facts = factsFor(
      ev(1, "gate_approved", {
        decided_by: "teammate",
        qualified_as: "permission",
        permission: "admin",
      }),
    );
    expect(facts.primary).toBe("by teammate (admin)");
  });

  it("names a named approver alone", () => {
    const facts = factsFor(
      ev(1, "gate_approved", { decided_by: "reviewer-1", qualified_as: "named", permission: null }),
    );
    expect(facts.primary).toBe("by reviewer-1");
  });

  it("adds the role to a review's rejection", () => {
    const facts = factsFor(
      ev(1, "gate_rejected", {
        reason: "changes requested by teammate",
        qualified_as: "permission",
        permission: "write",
      }),
    );
    expect(facts.primary).toBe("changes requested by teammate (write)");
  });
});

describe("an ignored review", () => {
  it("says whose review did not count and why, as something still waiting", () => {
    const facts = factsFor(
      ev(1, "gate_review_ignored", {
        pr_number: 248,
        reviewer: "teammate-2",
        verdict: "approved",
        reason: "below_permission",
        permission: "read",
        required: "write",
      }),
    );
    expect(facts.label).toBe("Review not counted");
    expect(facts.primary).toBe("approval by teammate-2 (read access, needs write)");
    expect(facts.tone).toBe("attention");
    expect(facts.significant).toBe(true);
    expect(linkFor(ev(2, "gate_review_ignored", { pr_number: 248 }), REPO)?.label).toBe("PR #248");
  });

  it("names each reason in words", () => {
    const why = (reason: string, extra: Record<string, unknown> = {}) =>
      factsFor(
        ev(1, "gate_review_ignored", {
          reviewer: "x",
          verdict: "changes_requested",
          reason,
          ...extra,
        }),
      ).primary;
    expect(why("not_named")).toBe("changes requested by x (not named in approvers)");
    expect(why("bot")).toBe("changes requested by x (a bot not named in approvers)");
    expect(why("app")).toBe("changes requested by x (the App's own review)");
    expect(why("below_permission", { permission: "none", required: "write" })).toBe(
      "changes requested by x (no access, needs write)",
    );
  });
});

describe("session and terminal facts", () => {
  it("folds a writer's consecutive typing into one row, and keeps two writers apart", () => {
    const typed = (id: number, viewer: string, bytes: number) =>
      ev(id, "terminal_input", { step: "plan", attempt: 1, by: "a@x.dev", viewer, bytes });
    const [segment] = segmentsFrom(
      [typed(1, "v1", 3), typed(2, "v1", 4), typed(3, "v2", 5), typed(4, "v1", 1)],
      [],
    );
    expect(segment?.entries.map((entry) => [entry.id, entry.details.bytes])).toEqual([
      [1, 7],
      [3, 5],
      [4, 1],
    ]);
  });

  it("says who took the keyboard, and from whom", () => {
    const taken = (from: string | null) =>
      factsFor(ev(1, "terminal_seat_taken", { by: "operator@x.dev", from, forced: from !== null }));
    expect(taken("teammate-2@x.dev").label).toBe("Typing taken over");
    expect(taken("teammate-2@x.dev").primary).toBe(
      "operator@x.dev took over typing from teammate-2@x.dev",
    );
    expect(taken(null).primary).toBe("operator@x.dev started typing");
  });

  it("names both ways to release an idle park, label removal and Resume, as the comment does", () => {
    const idle = ev(1, "agent_parked", { reason: "idle" });
    expect(factsFor(idle, true).primary).toBe(
      "To resume, press Resume or remove the factorial:waiting label from the issue",
    );
  });

  it("offers Resume beside the label for a rollover whose resume was refused", () => {
    const parked = ev(1, "agent_parked", { reason: "rollover" });
    expect(factsFor(parked, true).primary).toBe(
      "To resume, press Resume or remove the factorial:interrupted label from the issue",
    );
  });

  it("says who pressed Resume and whether they answered, never the answer", () => {
    const facts = factsFor(
      ev(1, "session_resume_requested", {
        by: "operator@example.com",
        has_answer: true,
        answer: "the password is hunter2",
      }),
    );
    expect(facts.label).toBe("Resume requested");
    expect(facts.primary).toBe("by operator@example.com, with an answer");
    expect(JSON.stringify(facts)).not.toContain("hunter2");
    expect(facts.significant).toBe(true);
    expect(
      factsFor(ev(2, "session_resume_requested", { by: "operator@example.com" })).primary,
    ).toBe("by operator@example.com");
  });

  it("says a dashboard Resume came from the dashboard", () => {
    const facts = factsFor(
      ev(1, "agent_resumed", { via: "dashboard", request_id: 4, comment_count: 0 }),
    );
    expect(facts.primary).toBe("from the dashboard");
  });

  it("follows a session view from request to close, none of it significant", () => {
    const requested = factsFor(
      ev(1, "inspect_requested", { requested_by: "operator@example.com" }),
    );
    expect(requested.label).toBe("Session requested");
    expect(requested.primary).toBe("by operator@example.com");
    expect(factsFor(ev(2, "inspect_started", { requested_by: "operator@example.com" })).label).toBe(
      "Session opened",
    );
    const asked = factsFor(
      ev(6, "inspect_close_requested", { requested_by: "operator@example.com" }),
    );
    expect(asked.label).toBe("Session close requested");
    expect(asked.primary).toBe("by operator@example.com");
    const closed = factsFor(ev(3, "inspect_parked", { reason: "the session ended" }));
    expect(closed.label).toBe("Session closed");
    expect(closed.primary).toBe("the session ended");
    const refused = factsFor(
      ev(4, "inspect_parked", { reason: "the session failed to start", failed: true }),
    );
    expect(refused.label).toBe("Session failed");
    expect(refused.tone).toBe("attention");
    for (const type of [
      "terminal_attached",
      "terminal_detached",
      "terminal_input",
      "terminal_seat_taken",
      "inspect_requested",
      "inspect_started",
      "inspect_close_requested",
      "inspect_parked",
    ]) {
      expect(factsFor(ev(5, type, {})).significant).toBe(false);
    }
  });
});

describe("restart (#321)", () => {
  const FROM = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0";
  const TO = "f0e1d2c3b4a5968778695a4b3c2d1e0f9a8b7c6d";
  // Generation 1 ran `plan` once; two restarts each put the ticket back on `plan`. The worker
  // appends `ticket_restarted` ahead of the `step_entered` that opens the new generation.
  const segments = segmentsFrom(
    [
      ev(1, "ticket_admitted", { step: "plan" }),
      ev(2, "step_started", { step: "plan", attempt: 1 }),
      ev(3, "ticket_restarted", { step: "plan", attempt: null, from_sha: FROM, to_sha: TO }),
      ev(4, "step_entered", { step: "plan", attempt: 2, reason: "restart" }),
      ev(5, "ticket_restarted", { step: "plan", attempt: null, from_sha: TO, to_sha: FROM }),
      ev(6, "step_entered", { step: "plan", attempt: 3, reason: "restart" }),
    ],
    [],
  );

  it("gives each restart a block of its own on the entry node, ahead of the attempt it starts", () => {
    expect(segments.map((s) => s.key)).toEqual([
      ENTRY_NODE_ID,
      "plan#1",
      "restart@3",
      "plan#2",
      "restart@5",
      "plan#3",
    ]);
    expect(
      segments.filter((s) => s.step === "restart").map((s) => [s.nodeId, s.attempt, s.kind, s.run]),
    ).toEqual([
      [ENTRY_NODE_ID, null, "entry", null],
      [ENTRY_NODE_ID, null, "entry", null],
    ]);
  });

  it("lands a click on the entry node on the newest way in, and walks back from there", () => {
    expect(attemptKeyForNode(segments, ENTRY_NODE_ID, null)).toBe("restart@5");
    expect(attemptKeyForNode(segments, ENTRY_NODE_ID, "restart@5")).toBe("restart@3");
  });

  describe("an attempt number two generations share", () => {
    // Generation 1 failed `plan` #1 and booked #2; the restart came before #2 ran, so generation
    // 2's first run of `plan` is #2 as well, since numbers count from the rows.
    const booked = [
      ev(1, "ticket_admitted", { step: "plan" }),
      ev(2, "step_started", { step: "plan", attempt: 1 }),
      ev(3, "agent_failed", { step: "plan", attempt: 1 }),
      ev(4, "retry_scheduled", { step: "plan", attempt: 1, next_attempt: 2, due_at: "x" }),
      ev(5, "ticket_restarted", { step: "plan", attempt: null, generation: 2 }),
    ];
    const old = run("plan", 1, "failed", { generation: 1 });
    const fresh = run("plan", 2, "running", { generation: 2 });
    const shape = (segments: ActivitySegment[]) =>
      segments.map((s) => [s.key, s.generation, s.run?.id ?? null, s.entries.map((e) => e.id)]);

    it("draws the booking and the new run as two blocks, each on its own side of the restart", () => {
      const segments = segmentsFrom(
        [
          ...booked,
          ev(6, "step_entered", { step: "plan", attempt: 2, reason: "restart" }),
          ev(7, "step_started", { step: "plan", attempt: 2 }),
        ],
        [old, fresh],
      );
      expect(shape(segments)).toEqual([
        [ENTRY_NODE_ID, 1, null, [1]],
        ["plan#1", 1, 100, [2, 3]],
        ["plan#2@g1", 1, null, [4]],
        ["restart@5", 2, null, [5]],
        ["plan#2", 2, 200, [6, 7]],
      ]);
    });

    it("keeps the new run apart while its events have not arrived", () => {
      expect(shape(segmentsFrom(booked, [old, fresh])).slice(2)).toEqual([
        ["plan#2@g1", 1, null, [4]],
        ["restart@5", 2, null, [5]],
        ["plan#2", 2, 200, []],
      ]);
    });

    it("files an earlier run's late event under that run, not the generation it arrived in", () => {
      const late = ev(6, "preview_url_updated", { step: "plan", attempt: 1, urls: [] });
      const segments = segmentsFrom([...booked, late], [old]);
      expect(shape(segments).map(([key, , , ids]) => [key, ids])).toEqual([
        [ENTRY_NODE_ID, [1]],
        ["plan#1", [2, 3, 6]],
        ["plan#2", [4]],
        ["restart@5", [5]],
      ]);
    });
  });

  it("names each event plainly", () => {
    expect(factsFor(ev(1, "restart_requested", {})).label).toBe("Restart requested");
    expect(factsFor(ev(2, "ticket_restarted", {})).label).toBe("Restarted");
    expect(factsFor(ev(3, "restart_refused", {})).label).toBe("Restart refused");
  });

  it("says what a request asked for, and where it came from", () => {
    const asked = factsFor(
      ev(1, "restart_requested", { target_sha: TO, requested_via: "dashboard" }),
    );
    expect(asked.primary).toBe("onto f0e1d2c, from the dashboard");
    expect(asked.tone).toBe("neutral");
  });

  it("reads a restart as the boundary it is: the lane, both commits, and who asked", () => {
    const restarted = factsFor(
      ev(1, "ticket_restarted", {
        process: "standard",
        from_sha: FROM,
        to_sha: TO,
        requested_by: "operator@example.com",
        requested_via: "dashboard",
        cancelled: null,
        abandoned: null,
        closed_prs: [],
        deleted_branches: [],
      }),
    );
    expect(restarted.primary).toBe(
      "on standard @ f0e1d2c (was a1b2c3d) · by operator@example.com from the dashboard",
    );
    expect(restarted.tone).toBe("neutral");
    expect(restarted.significant).toBe(true);
    expect(restarted.long).toBeNull();
  });

  it("calls the session a restart closed paused, not parked", () => {
    const restarted = factsFor(
      ev(1, "ticket_restarted", { to_sha: TO, abandoned: { step: "plan", attempt: 1 } }),
    );
    expect(restarted.long?.body).toBe("Closed the paused session of plan attempt 1");
  });

  it("folds what the restart removed behind a click", () => {
    const restarted = factsFor(
      ev(1, "ticket_restarted", {
        from_sha: FROM,
        to_sha: TO,
        cancelled: { step: "impl", attempt: 2 },
        abandoned: null,
        closed_prs: [241, 239],
        deleted_branches: ["impl/204-remit-to", "spec/204-remit-to"],
      }),
    );
    expect(restarted.primary).toBe("on f0e1d2c (was a1b2c3d)");
    expect(restarted.long).toEqual({
      title: "What the restart removed",
      body: "Canceled impl attempt 2\nClosed pull requests #241, #239\nDeleted branches impl/204-remit-to, spec/204-remit-to",
    });
  });

  it("says why a restart was refused, in amber, and keeps the worker's detail", () => {
    const words: [string, string][] = [
      ["issue_closed", "the issue is closed; reopen it first"],
      ["lane_retired", "this process was removed, so there is nothing to restart on"],
      ["merged_outside", "the ticket's work is already merged; restart again and confirm it"],
      ["cancel_failed", "the run in flight couldn't be confirmed stopped; exit, then restart"],
      ["exited", "the ticket exited first; restart it from there"],
    ];
    for (const [reason, primary] of words) {
      expect(factsFor(ev(1, "restart_refused", { reason })).primary, reason).toBe(primary);
    }
    const refused = factsFor(
      ev(1, "restart_refused", {
        reason: "merged_outside",
        detail: "pull request #239 was merged into main",
      }),
    );
    expect(refused.tone).toBe("attention");
    expect(refused.long).toEqual({
      title: "Why the restart was refused",
      body: "pull request #239 was merged into main",
    });
  });

  it("says an entry was a restart, not an ordinary advance", () => {
    const entered = ev(1, "step_entered", { step: "plan", from_step: "impl", reason: "restart" });
    expect(factsFor(entered).primary).toBe("restart, from impl");
    expect(factsFor(ev(2, "step_entered", { step: "plan", reason: "restart" })).primary).toBe(
      "restart",
    );
  });
});

describe("redo (decision 76)", () => {
  // `plan` #1 failed and its backoff booked #2; a redo came before #2 ran, so the fresh attempt
  // is #2 as well. The worker appends `step_redone` ahead of the `step_entered` it starts.
  const entries = [
    ev(1, "step_started", { step: "plan", attempt: 1 }),
    ev(2, "agent_failed", { step: "plan", attempt: 1 }),
    ev(3, "retry_scheduled", { step: "plan", attempt: 1, next_attempt: 2, due_at: "x" }),
    ev(4, "redo_requested", { step: "plan", attempt: 1, target_step: "plan", note: "v2" }),
    ev(5, "step_redone", {
      step: "plan",
      attempt: null,
      target_step: "plan",
      discarded_steps: ["plan", "spec_gate"],
      request_id: 4,
      requested_by: "operator",
    }),
    ev(6, "step_entered", { step: "plan", attempt: 2, reason: "redo" }),
    ev(7, "step_started", { step: "plan", attempt: 2 }),
  ];
  const runs = [
    run("plan", 1, "cancelled", { superseded_at: "2026-08-25T09:10:00Z" }),
    run("plan", 2, "running"),
  ];

  it("puts the fresh attempt after the redo, and the overtaken booking before it", () => {
    const newest = sortSegments(segmentsFrom(entries, runs), "newest");
    expect(
      newest.map((s) => [s.key, s.run?.id ?? null, s.status, s.entries.map((e) => e.id)]),
    ).toEqual([
      ["plan#2", 200, "running", [7, 6]],
      ["redo@5", null, "info", [5, 4]],
      ["plan#2@r0", null, "info", [3]],
      ["plan#1", 100, "set_aside", [2, 1]],
    ]);
    expect(cancelledRetries(entries).get(3)).toBe("redone by operator");
  });
});
