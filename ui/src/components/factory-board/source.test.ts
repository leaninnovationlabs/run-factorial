import { describe, expect, it } from "vitest";
import type { OverviewResponse } from "../../api/types";
import { laneOf } from "./model";
import { toBoard } from "./source";

const SANDBOX = { id: 7, name: "widgets", active: true };

const wire = (over: Partial<OverviewResponse["tickets"][number]> = {}) => ({
  ticket_id: 1,
  project_id: 7,
  external_key: "acme/repo#12",
  title: "Retry the webhook",
  current_step: "build",
  process_key: "standard",
  latest_run_status: null,
  park_reason: null,
  status: "running" as const,
  runs: 3,
  attempt: 1,
  cost_usd: "4.25",
  cost_estimated: false,
  tokens: 120_000,
  minutes: 45.5,
  quarantined_at: null,
  ...over,
});

describe("toBoard", () => {
  it("carries every figure the board shades by across the wire boundary", () => {
    const { nodes } = toBoard({ projects: [SANDBOX], tickets: [wire()], done_days: 7 });
    expect(nodes[0]).toEqual({
      id: 1,
      key: "acme/repo#12",
      title: "Retry the webhook",
      projectId: 7,
      step: "build",
      process: "standard",
      status: "running",
      quarantined: false,
      latestRunStatus: null,
      parkReason: null,
      sandboxWaiting: false,
      runs: 3,
      attempt: 1,
      costUsd: 4.25,
      costEstimated: false,
      minutes: 45.5,
      tokens: 120_000,
    });
  });

  it("collapses quarantined_at to a flag, not the timestamp itself", () => {
    // The board's lane cares only whether the loop has stopped leasing the ticket, not when.
    const { nodes } = toBoard({
      projects: [SANDBOX],
      tickets: [wire({ quarantined_at: "2026-09-04T10:00:00Z" })],
      done_days: 7,
    });
    expect(nodes[0]?.quarantined).toBe(true);
  });

  it("flags a ticket that waits for a sandbox", () => {
    const wait = { scope: "pool" as const, limit: 3, running: 3, runtime: "r", runtime_id: 1 };
    const { nodes } = toBoard({
      projects: [SANDBOX],
      tickets: [wire({ sandbox_wait: wait })],
      done_days: 7,
    });
    expect(nodes[0]?.sandboxWaiting).toBe(true);
  });

  it("reads an un-quarantined ticket as false", () => {
    const { nodes } = toBoard({ projects: [SANDBOX], tickets: [wire()], done_days: 7 });
    expect(nodes[0]?.quarantined).toBe(false);
  });

  it("carries a null title through rather than inventing one", () => {
    // Every ticket admitted before the observer stored titles, and every one already done when
    // it shipped. The row falls back to the issue number for these.
    const { nodes } = toBoard({
      projects: [SANDBOX],
      tickets: [wire({ title: null })],
      done_days: 7,
    });
    expect(nodes[0]?.title).toBeNull();
  });

  it("reads cost as a number, because the wire sends a decimal string", () => {
    // `cost_usd` is a Decimal serialized as a string so it keeps its scale. Left as a string it
    // would sort lexically and sum by concatenation, and both failures look like data problems.
    const { nodes } = toBoard({
      projects: [SANDBOX],
      tickets: [wire({ cost_usd: "10.00" })],
      done_days: 7,
    });
    expect(nodes[0]?.costUsd).toBe(10);
  });

  it("keeps a ticket that has never run in the admitted lane", () => {
    const { nodes } = toBoard({
      projects: [SANDBOX],
      tickets: [wire({ runs: 0, attempt: null })],
      done_days: 7,
    });
    expect(nodes.map(laneOf)).toEqual(["admitted"]);
  });

  it("drops a ticket whose project the response does not carry", () => {
    // The board draws bands from `projects`; a node with no band has nowhere to go, and `layout`
    // would silently drop it anyway. Dropping it here makes the count on the stats panel honest.
    const { nodes } = toBoard({
      projects: [SANDBOX],
      tickets: [wire(), wire({ ticket_id: 2, project_id: 99 })],
      done_days: 7,
    });
    expect(nodes.map((n) => n.id)).toEqual([1]);
  });

  it("passes the projects through in the order the API ranked them", () => {
    const { projects } = toBoard({
      projects: [
        { id: 7, name: "widgets", active: true },
        { id: 2, name: "payments-api", active: true },
      ],
      tickets: [],
      done_days: 7,
    });
    expect(projects).toEqual([
      { id: 7, name: "widgets" },
      { id: 2, name: "payments-api" },
    ]);
  });
});
