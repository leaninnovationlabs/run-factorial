import { describe, expect, it } from "vitest";
import {
  CONDITION_LABELS,
  CONDITION_TONES,
  conditionOf,
  latestRunStatusAt,
  runTone,
  type TicketCondition,
} from "./status";

const at = (step: string, attempt: number, status: string) =>
  ({ step_name: step, attempt, status }) as const;

describe("latestRunStatusAt", () => {
  it("reads the highest attempt at the step, not the last row", () => {
    const runs = [at("plan", 2, "interrupted"), at("plan", 1, "failed"), at("build", 1, "running")];
    expect(latestRunStatusAt(runs, "plan")).toBe("interrupted");
  });

  it("is null for a step with no runs yet", () => {
    expect(latestRunStatusAt([at("plan", 1, "running")], "build")).toBeNull();
  });
});

describe("conditionOf", () => {
  it("splits blocked on why the ticket parked", () => {
    const blocked = (latestRunStatus: string | null): TicketCondition =>
      conditionOf({ status: "blocked", latestRunStatus, runs: 3, attempt: 1 });
    expect(blocked("interrupted")).toBe("interrupted");
    expect(blocked("rejected")).toBe("rejected");
    expect(blocked("failed")).toBe("blocked");
    expect(blocked("timeout")).toBe("blocked");
    expect(blocked("cancelled")).toBe("blocked");
    // A parked ticket whose run row has not arrived on this poll is still parked.
    expect(blocked(null)).toBe("blocked");
  });

  it("tells a ticket waiting on a human from one a human stopped", () => {
    // An idle park (decision 56) and a human's Interrupt both leave the run `interrupted` and the
    // ticket `blocked`; only the park's reason says which.
    const parked = (parkReason: string | null | undefined, latestRunStatus = "interrupted") =>
      conditionOf({ status: "blocked", latestRunStatus, parkReason });
    expect(parked("idle")).toBe("waiting_human");
    expect(parked("user")).toBe("interrupted");
    expect(parked("rollover")).toBe("interrupted");
    expect(parked(null)).toBe("interrupted");
    expect(parked(undefined)).toBe("interrupted");
    // The reason qualifies a parked run, not whatever ended the latest attempt.
    expect(parked("idle", "failed")).toBe("blocked");
  });

  it("keeps a live idle session running until the loop parks it", () => {
    expect(conditionOf({ status: "running", latestRunStatus: "idle", runs: 1, attempt: 1 })).toBe(
      "running",
    );
  });

  it("keeps the other three ticket statuses whole", () => {
    expect(conditionOf({ status: "waiting_gate", latestRunStatus: "waiting" })).toBe(
      "waiting_gate",
    );
    expect(conditionOf({ status: "done", latestRunStatus: "approved" })).toBe("done");
    expect(
      conditionOf({ status: "running", latestRunStatus: "running", runs: 1, attempt: 1 }),
    ).toBe("running");
  });

  it("separates admitted and retrying out of running", () => {
    expect(conditionOf({ status: "running", latestRunStatus: null, runs: 0, attempt: null })).toBe(
      "admitted",
    );
    expect(
      conditionOf({ status: "running", latestRunStatus: "running", runs: 4, attempt: 2 }),
    ).toBe("retrying");
  });

  it("does not read run counts it was not given — the ticket page has no aggregate", () => {
    expect(conditionOf({ status: "running", latestRunStatus: "running" })).toBe("running");
  });

  it("reads exited straight off the status, whatever the last run says", () => {
    // Exit cancels live work, so the latest run is usually `cancelled` — which under `blocked`
    // would mean an escalation. The status is the fact; the run is history.
    expect(conditionOf({ status: "exited", latestRunStatus: "cancelled" })).toBe("exited");
    expect(conditionOf({ status: "exited", latestRunStatus: null, runs: 0 })).toBe("exited");
  });

  it("reads quarantined off the flag first, whatever status or run the ticket carries", () => {
    // Quarantine is set on TOP of a status, not instead of it (decision 31) — a quarantined
    // ticket can be done, blocked-and-interrupted, or mid-retry, and the reader still needs to
    // be told it is out of the loop before anything else.
    expect(conditionOf({ status: "done", latestRunStatus: null, quarantined: true })).toBe(
      "quarantined",
    );
    expect(
      conditionOf({ status: "blocked", latestRunStatus: "interrupted", quarantined: true }),
    ).toBe("quarantined");
    expect(
      conditionOf({
        status: "running",
        latestRunStatus: "running",
        runs: 4,
        attempt: 2,
        quarantined: true,
      }),
    ).toBe("quarantined");
  });

  it("falls through to the ordinary derivation when the flag is false or absent", () => {
    expect(conditionOf({ status: "done", latestRunStatus: null, quarantined: false })).toBe("done");
    expect(conditionOf({ status: "done", latestRunStatus: null })).toBe("done");
  });
});

describe("the tone table", () => {
  it("gives waiting_gate and interrupted the same ink", () => {
    expect(CONDITION_TONES.interrupted).toBe(CONDITION_TONES.waiting_gate);
  });

  it("gives waiting on a human the same ink as waiting at a gate", () => {
    expect(CONDITION_TONES.waiting_human).toBe(CONDITION_TONES.waiting_gate);
  });

  it("draws sent away apart from finished", () => {
    // Both are neutral lanes on the board, and both set `closed_at` — but an exited ticket can
    // come back, so a reader must not have to check the label to tell them apart.
    expect(CONDITION_TONES.exited).not.toBe(CONDITION_TONES.done);
  });

  it("keeps rejected apart from blocked", () => {
    expect(CONDITION_TONES.rejected).toBe("rejected");
    expect(CONDITION_TONES.blocked).toBe("error");
  });

  it("gives quarantined the same error ink as an escalation", () => {
    expect(CONDITION_TONES.quarantined).toBe("error");
  });

  it("labels every condition", () => {
    for (const condition of Object.keys(CONDITION_TONES) as TicketCondition[]) {
      expect(CONDITION_LABELS[condition]).toBeTruthy();
    }
  });

  it("agrees with the run status it is derived from", () => {
    // The whole point of one table: an interrupted attempt and the ticket it parked read the same.
    expect(runTone("interrupted")).toBe(CONDITION_TONES.interrupted);
    expect(runTone("rejected")).toBe(CONDITION_TONES.rejected);
    expect(runTone("failed")).toBe(CONDITION_TONES.blocked);
    expect(runTone("waiting")).toBe(CONDITION_TONES.waiting_gate);
    expect(runTone("running")).toBe(CONDITION_TONES.running);
    expect(runTone("succeeded")).toBe(CONDITION_TONES.done);
    expect(runTone("approved")).toBe(CONDITION_TONES.done);
  });

  it("gives cancelled the neutral ink — nothing is asked of anyone", () => {
    expect(runTone("cancelled")).toBe("neutral");
  });
});
