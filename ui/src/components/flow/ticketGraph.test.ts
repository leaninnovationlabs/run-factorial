import { describe, expect, it } from "vitest";
import { ENTRY_NODE_ID } from "./processGraph";
import { attemptBadges, attemptCountsFromRuns, nodeTones, visitedNodeIds } from "./ticketGraph";

describe("attemptCountsFromRuns", () => {
  it("takes the highest attempt number per step", () => {
    expect(
      attemptCountsFromRuns([
        { step_name: "plan", attempt: 1 },
        { step_name: "plan", attempt: 2 },
        { step_name: "spec_gate", attempt: 2 },
        { step_name: "build", attempt: 1 },
      ]),
    ).toEqual({ plan: 2, spec_gate: 2, build: 1 });
  });

  it("is empty when nothing has run", () => {
    expect(attemptCountsFromRuns([])).toEqual({});
  });
});

describe("attemptBadges", () => {
  const RUNS = [
    { step_name: "plan", attempt: 1 },
    { step_name: "plan", attempt: 2 },
    { step_name: "plan", attempt: 3 },
    { step_name: "spec_gate", attempt: 1 },
  ];

  it("says how many times each step has run when the reader is elsewhere", () => {
    expect(attemptBadges(RUNS, null)).toEqual({ plan: "3", spec_gate: "1" });
  });

  it("says which attempt the reader is in on the step they are reading", () => {
    expect(attemptBadges(RUNS, { nodeId: "plan", attempt: 2 })).toEqual({
      plan: "2/3",
      spec_gate: "1",
    });
  });

  it("keeps the total when the reader is in the most recent attempt", () => {
    // Not "3" — which attempt you are in is a different fact from how many there are, and the
    // badge has to keep saying both once it is saying either.
    expect(attemptBadges(RUNS, { nodeId: "plan", attempt: 3 })?.plan).toBe("3/3");
  });

  it("leaves a step that has only ever run once alone", () => {
    expect(attemptBadges(RUNS, { nodeId: "spec_gate", attempt: 1 })?.spec_gate).toBe("1");
  });

  it("badges nothing for admission, which is not an attempt at anything", () => {
    expect(attemptBadges(RUNS, { nodeId: ENTRY_NODE_ID, attempt: null })).toEqual({
      plan: "3",
      spec_gate: "1",
    });
  });

  it("trusts the attempt being read over a step_run poll that has not caught up", () => {
    // The timeline knows about attempt 4; the ticket poll still only has three runs.
    expect(attemptBadges(RUNS, { nodeId: "plan", attempt: 4 })?.plan).toBe("4/4");
  });

  it("has nothing to say before anything has run", () => {
    expect(attemptBadges([], { nodeId: "plan", attempt: 1 })).toEqual({});
  });
});

describe("visitedNodeIds", () => {
  it("marks entry, any run step, and the current step as visited", () => {
    expect([...visitedNodeIds([{ step_name: "plan" }], "spec_gate")].sort()).toEqual(
      [ENTRY_NODE_ID, "plan", "spec_gate"].sort(),
    );
  });
});

describe("nodeTones", () => {
  it("marks each step by how its LATEST attempt ended", () => {
    // A step that failed and then passed on the retry ended well and carries no mark; the two
    // that did not each report what actually happened, in tones of their own.
    expect(
      nodeTones([
        { step_name: "plan", attempt: 1, status: "failed" },
        { step_name: "plan", attempt: 2, status: "succeeded" },
        { step_name: "build", attempt: 1, status: "timeout" },
        { step_name: "spec_gate", attempt: 1, status: "rejected" },
      ]),
    ).toEqual({ build: "error", spec_gate: "rejected" });
  });

  it("says nothing about a step that simply ran and passed", () => {
    expect(nodeTones([{ step_name: "plan", attempt: 1, status: "succeeded" }])).toEqual({});
  });

  it("marks a step a person forced past its failed conditions, which reads succeeded", () => {
    // Unmarked, the graph would show a step whose checks failed as one that passed.
    expect(
      nodeTones([
        { step_name: "plan", attempt: 1, status: "succeeded", forced_by: "op@example.com" },
      ]),
    ).toEqual({ plan: "attention" });
  });

  it("paints an interrupted ticket's step in the waiting tone, not the error one", () => {
    // The case the whole vocabulary exists for: the run is finished and the ticket is parked with
    // `status = blocked`, and the node has to say "waiting on you" rather than "this broke".
    expect(
      nodeTones([{ step_name: "plan", attempt: 2, status: "interrupted" }], {
        status: "blocked",
        current_step: "plan",
      }),
    ).toEqual({ plan: "attention" });
  });

  it("paints an open gate the ticket is sitting at in the same tone", () => {
    expect(
      nodeTones([{ step_name: "spec_gate", attempt: 1, status: "waiting" }], {
        status: "waiting_gate",
        current_step: "spec_gate",
      }),
    ).toEqual({ spec_gate: "attention" });
  });

  it("keeps an escalation red", () => {
    expect(
      nodeTones([{ step_name: "plan", attempt: 3, status: "failed" }], {
        status: "blocked",
        current_step: "plan",
      }),
    ).toEqual({ plan: "error" });
  });

  it("clears the mark on a step the ticket is running again", () => {
    // A retry after a failure: the step is under way, and colouring it by how the previous try
    // ended would report a state the ticket has already left.
    expect(
      nodeTones([{ step_name: "plan", attempt: 1, status: "failed" }], {
        status: "running",
        current_step: "plan",
      }),
    ).toEqual({});
  });

  it("still marks the steps behind a running ticket", () => {
    // A gate that was rejected and looped back to `plan`: the ticket moved on, but the gate is
    // not the clean success an unmarked node would draw as.
    expect(
      nodeTones(
        [
          { step_name: "plan", attempt: 1, status: "succeeded" },
          { step_name: "spec_gate", attempt: 1, status: "rejected" },
          { step_name: "plan", attempt: 2, status: "running" },
        ],
        { status: "running", current_step: "plan" },
      ),
    ).toEqual({ spec_gate: "rejected" });
  });
});
