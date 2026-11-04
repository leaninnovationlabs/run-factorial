import { describe, expect, it } from "vitest";
import type { StepRun, TimelineEntry } from "../../api/types";
import { run } from "../../pages/TicketPage.fixture";
import { cancelledRetries, retryOffer } from "./retry";

const DUE = "2026-08-25T09:34:18+00:00";
const LATER = "2026-08-25T09:44:18+00:00";

function ev(id: number, type: string, details: Record<string, unknown>): TimelineEntry {
  return { id, type, details, created_at: "2026-08-25T09:24:18Z" };
}

const failed = run(7, "plan", 1, "failed") as StepRun;
const running = { current_step: "plan", status: "running" } as const;
const scheduled = ev(41, "retry_scheduled", {
  step: "plan",
  attempt: 1,
  next_attempt: 2,
  due_at: DUE,
});

describe("retryOffer", () => {
  const blocked = { current_step: "plan", status: "blocked" } as const;
  const escalated = ev(44, "escalated", { step: "plan", attempt: 1 });

  it("offers the failed run during its backoff, with when the ladder would start it", () => {
    expect(retryOffer(running, [failed], [scheduled])).toEqual({ run: failed, due: DUE });
  });

  it("offers the failed run of an escalated step, with no due time", () => {
    expect(retryOffer(blocked, [failed], [escalated])).toEqual({ run: failed, due: null });
  });

  it("offers nothing when a refused spawn blocked the ticket after the failure", () => {
    const refused = ev(50, "spawn_refused", { step: "plan" });
    expect(retryOffer(blocked, [failed], [escalated, refused])).toBeNull();
  });

  it("offers nothing once the next attempt started, or for a gate", () => {
    expect(
      retryOffer(running, [failed, run(8, "plan", 2, "running") as StepRun], [scheduled]),
    ).toBeNull();
    expect(retryOffer(blocked, [{ ...failed, kind: "human_gate" }], [escalated])).toBeNull();
  });
});

describe("cancelledRetries", () => {
  const started = (id: number, attempt: number) =>
    ev(id, "step_started", { step: "plan", attempt, kind: "agent" });

  it("cancels the countdown a Retry now overtook, and names who pressed it", () => {
    const asked = ev(45, "retry_requested", {
      step: "plan",
      attempt: 1,
      next_attempt: 2,
      requested_by: "teammate",
    });
    expect(cancelledRetries([scheduled, asked, started(46, 2)]).get(41)).toBe(
      "retried now by teammate",
    );
  });

  it("cancels a booking a restart or an exit came before", () => {
    const restarted = ev(50, "ticket_restarted", { requested_by: "teammate" });
    expect(cancelledRetries([scheduled, restarted]).get(41)).toBe("restarted by teammate");
    const exited = ev(50, "ticket_exited", { step: "plan", attempt: 1, via: "dashboard" });
    expect(cancelledRetries([scheduled, exited]).get(41)).toBe("the ticket exited");
  });

  it("leaves a booking the retry redeemed, whatever came after it", () => {
    const restarted = ev(60, "ticket_restarted", { requested_by: "teammate" });
    expect(cancelledRetries([scheduled, started(46, 2), restarted]).size).toBe(0);
  });

  it("leaves a booking a Retry now for another attempt did not touch", () => {
    const other = ev(45, "retry_requested", { step: "build", attempt: 1, next_attempt: 2 });
    expect(cancelledRetries([scheduled, other]).size).toBe(0);
  });

  it("cancels the countdown when the step is forced or accepted, though the row names the failed attempt", () => {
    const forced = ev(60, "step_forced", { step: "plan", attempt: 1, requested_by: "operator" });
    expect(cancelledRetries([scheduled, forced]).get(scheduled.id)).toBe(
      "forced complete by operator",
    );
  });

  it("keeps counting down when the loop refused the Retry pressed during the backoff", () => {
    const asked = ev(45, "step_retry_requested", { step: "plan", attempt: 1, next_attempt: 2 });
    const refused = ev(46, "step_retry_refused", { step: "plan", request_id: 45 });
    expect(cancelledRetries([scheduled, asked, refused]).size).toBe(0);
    expect(cancelledRetries([scheduled, asked]).get(41)).toBe("retried now");
  });

  it("cancels the wait an exit cut short, and counts the re-entry's down", () => {
    const exited = ev(50, "ticket_exited", { step: "plan", attempt: 1, via: "label" });
    const reentered = ev(51, "ticket_reentered", {
      step: "plan",
      attempt: 1,
      next_attempt: 2,
      due_at: LATER,
    });
    const cancelled = cancelledRetries([reentered, exited, scheduled]);
    expect([...cancelled.keys()]).toEqual([41]);
  });
});
