import { describe, expect, it } from "vitest";
import {
  bandOf,
  countLanes,
  DEFAULT_THRESHOLDS,
  heatOf,
  type IssueNode,
  LANE_HINTS,
  LANE_LABELS,
  LANES,
  laneOf,
  METRIC_SPECS,
  METRICS,
  metricOf,
  metricValue,
  modeButtons,
  rampBands,
  retriesOf,
  SHADE_MODES,
  summariseMetric,
} from "./model";

function node(over: Partial<IssueNode> = {}): IssueNode {
  return {
    id: 1,
    key: "acme/repo#1",
    title: null,
    projectId: 1,
    step: "build",
    latestRunStatus: null,
    status: "running",
    quarantined: false,
    runs: 1,
    attempt: 1,
    costUsd: 0,
    minutes: 0,
    tokens: 0,
    ...over,
  };
}

describe("laneOf", () => {
  it("reads the terminal states straight off the status", () => {
    expect(laneOf(node({ status: "done" }))).toBe("done");
    expect(laneOf(node({ status: "waiting_gate" }))).toBe("gate");
    expect(laneOf(node({ status: "blocked" }))).toBe("error");
  });

  it("files a ticket somebody interrupted under Gate, not Error", () => {
    // "Waiting on a human decision" is exactly what an interrupt is: it moves when the person who
    // stopped it removes the label. Error is for work that broke, and it parks with the same
    // `blocked` status, so only the run tells them apart.
    expect(laneOf(node({ status: "blocked", latestRunStatus: "interrupted" }))).toBe("gate");
  });

  it("leaves a rejected gate with nowhere to go in Error", () => {
    // The process has no `onReject` edge to follow, so nobody is being asked for a decision — it
    // is stopped, which is what the Error lane means.
    expect(laneOf(node({ status: "blocked", latestRunStatus: "rejected" }))).toBe("error");
  });

  it("keeps an escalation in Error whatever ended the run", () => {
    for (const status of ["failed", "timeout", "cancelled", null]) {
      expect(laneOf(node({ status: "blocked", latestRunStatus: status }))).toBe("error");
    }
  });

  it("ignores the latest run on a ticket that is not parked", () => {
    // A step that failed and is being retried is Retrying, not Error: the ticket's own status is
    // what says whether the run's ending still holds.
    expect(
      laneOf(node({ status: "running", latestRunStatus: "failed", runs: 3, attempt: 2 })),
    ).toBe("retrying");
  });

  it("calls a ticket with no runs at all admitted", () => {
    expect(laneOf(node({ status: "running", runs: 0, attempt: null }))).toBe("admitted");
  });

  it("does not mistake an advanced ticket for a re-running one", () => {
    // Three runs across three steps is not three attempts at one.
    expect(laneOf(node({ status: "running", runs: 3, attempt: 1 }))).toBe("running");
  });

  it("calls a second attempt at the current step a re-run", () => {
    expect(laneOf(node({ status: "running", runs: 4, attempt: 2 }))).toBe("retrying");
  });

  it("treats a running ticket with no in-flight attempt as a first attempt", () => {
    // Between two steps there is briefly no run to read an attempt from. Defaulting to
    // "re-running" there would flash the whole board orange every time a step advanced.
    expect(laneOf(node({ status: "running", runs: 2, attempt: null }))).toBe("running");
  });

  it("files a quarantined ticket under Error, whatever its status says", () => {
    // Quarantine is orthogonal to status (decision 31): the loop has given up on this ticket,
    // which is exactly what Error means, whether it was waiting at a gate or already done.
    expect(laneOf(node({ status: "waiting_gate", quarantined: true }))).toBe("error");
    expect(laneOf(node({ status: "done", quarantined: true }))).toBe("error");
  });

  it("seats an exited ticket with Done", () => {
    // Off the floor and asking nothing of anyone — the same claim Done makes. Not its own lane:
    // the board's lanes are what the factory is doing, and an exited ticket is not being done.
    expect(laneOf(node({ status: "exited", latestRunStatus: "cancelled" }))).toBe("done");
  });

  it("puts every ticket in exactly one lane", () => {
    const every: IssueNode[] = [
      node({ status: "running", runs: 0, attempt: null }),
      node({ status: "running", runs: 1, attempt: 1 }),
      node({ status: "running", runs: 5, attempt: 3 }),
      node({ status: "waiting_gate" }),
      node({ status: "blocked" }),
      node({ status: "done" }),
      node({ status: "exited", latestRunStatus: "cancelled" }),
    ];
    const lanes = every.map(laneOf);
    // One lane each, and between them every lane — exited seats with done, so the set of lanes
    // is one shorter than the list of tickets.
    expect(new Set(lanes)).toEqual(new Set(LANES));
    for (const lane of lanes) expect(LANES).toContain(lane);
  });
});

describe("retriesOf", () => {
  it("counts attempts beyond the first at the current step, never total runs", () => {
    expect(retriesOf(node({ runs: 9, attempt: 1 }))).toBe(0);
    expect(retriesOf(node({ runs: 9, attempt: 3 }))).toBe(2);
    expect(retriesOf(node({ runs: 9, attempt: null }))).toBe(0);
  });
});

describe("countLanes", () => {
  it("totals to the number of tickets, because the lanes are a partition", () => {
    const nodes = [
      node({ status: "done" }),
      node({ status: "done" }),
      node({ status: "blocked" }),
      node({ status: "running", runs: 0, attempt: null }),
    ];
    const counts = countLanes(nodes);
    expect(counts).toMatchObject({ done: 2, error: 1, admitted: 1, running: 0 });
    expect(Object.values(counts).reduce((a, b) => a + b, 0)).toBe(nodes.length);
  });
});

describe("modeButtons", () => {
  const button = (nodes: IssueNode[], id: string) =>
    modeButtons(SHADE_MODES, nodes).find((entry) => entry.id === id);

  it("gives a metric button two figures", () => {
    expect(button([node()], "shade_cost")?.figures).not.toBeNull();
  });

  it("reports the set it was given, which is what carries the focus onto the rail", () => {
    // Every figure is computed from the argument and nothing else, so the page can narrow the
    // whole rail to a focused lane, project or cell by narrowing this one array.
    const nodes = [node({ id: 1, minutes: 30 }), node({ id: 2, minutes: 90 })];
    expect(button(nodes, "shade_time")?.figures).toEqual({ total: "2h", max: "1h 30m" });
    expect(button([nodes[0] as IssueNode], "shade_time")?.figures).toEqual({
      total: "30m",
      max: "30m",
    });
  });

  it("stacks the total over the single worst ticket, never one without the other", () => {
    const nodes = [node({ id: 1, costUsd: 2 }), node({ id: 2, costUsd: 8 })];
    expect(button(nodes, "shade_cost")?.figures).toEqual({ total: "$10.00", max: "$8.00" });
  });

  it("writes cost to the cent however large, so the two lines are the same shape", () => {
    // See `formatCost` in `model.ts`.
    const nodes = [node({ id: 1, costUsd: 120 }), node({ id: 2, costUsd: 8 })];
    expect(button(nodes, "shade_cost")?.figures).toEqual({ total: "$128.00", max: "$120.00" });
  });

  it("gives both figures their own unit, neither one borrowing the other's", () => {
    // They are read as a column rather than as a sentence, and the button labels each line, so
    // a max that said `900k` under a total saying `1.0M tok` was leaning on its neighbour for
    // what it measured. Retries carry the noun that `Metric` draws as a mark.
    const nodes = [node({ id: 1, tokens: 100_000 }), node({ id: 2, tokens: 900_000 })];
    expect(button(nodes, "shade_tokens")?.figures).toEqual({ total: "1.0M tok", max: "900k tok" });
    expect(button([node({ attempt: 3 })], "shade_retry")?.figures).toEqual({
      total: "2 retries",
      max: "2 retries",
    });
  });

  it("distinguishes an empty selection from a selection that has spent nothing", () => {
    // Four buttons reading "$0.00 over $0.00 max" would look like an answer. They are not the
    // same statement, and a focus can land on an empty node group.
    expect(button([], "shade_cost")?.figures).toBeNull();
    expect(button([node({ costUsd: 0 })], "shade_cost")?.figures).toEqual({
      total: "$0.00",
      max: "$0.00",
    });
  });

  it("keeps the explanation on the button, for the tooltip the live line displaced", () => {
    expect(button([node()], "shade_tokens")?.hint).toBe(METRIC_SPECS.tokens.hint);
  });
});

describe("summariseMetric", () => {
  it("separates the total from the max, because a band can be bad in two ways", () => {
    const many = Array.from({ length: 10 }, (_, i) => node({ id: i + 1, costUsd: 1 }));
    const one = [node({ id: 99, costUsd: 10 })];
    expect(summariseMetric(many, "cost").total).toBe(summariseMetric(one, "cost").total);
    // Same sum, and only one of them is a ticket somebody can go and fix.
    expect(summariseMetric(many, "cost").max).toBe(1);
    expect(summariseMetric(one, "cost").max).toBe(10);
  });

  it("reports an empty set as empty rather than as zero spend", () => {
    expect(summariseMetric([], "cost")).toEqual({ nodes: 0, total: 0, max: 0 });
  });
});

describe("heatOf", () => {
  it("reproduces the fixed retry ramp it replaced, at the retry defaults", () => {
    // 1 and 3 were chosen to be exactly the old hard-coded bands, so making retries
    // configurable moved no pixel until somebody moved a number.
    const t = DEFAULT_THRESHOLDS.retry;
    expect([0, 1, 2, 3, 40].map((value) => heatOf(value, t))).toEqual([0, 1, 2, 3, 3]);
  });

  it("is open-ended at both ends, so contrast is spent where the answer changes", () => {
    const t = { lower: 10, upper: 20 };
    expect(heatOf(-5, t)).toBe(0);
    expect(heatOf(9.99, t)).toBe(0);
    expect(heatOf(20, t)).toBe(3);
    expect(heatOf(1e9, t)).toBe(3);
  });

  it("splits the span between the thresholds at its midpoint", () => {
    const t = { lower: 10, upper: 20 };
    expect(heatOf(14.9, t)).toBe(1);
    expect(heatOf(15, t)).toBe(2);
  });

  it("collapses to two bands rather than breaking when upper is not above lower", () => {
    // An operator can type this, and it is a coherent thing to ask for — everything is either
    // fine or bad — so it must not produce a band the stylesheet has no colour for.
    const t = { lower: 5, upper: 5 };
    expect([0, 4, 5, 500].map((value) => heatOf(value, t))).toEqual([0, 0, 3, 3]);
  });
});

describe("the metrics", () => {
  it("reads each metric off the node, with retries derived rather than stored", () => {
    const n = node({ attempt: 3, costUsd: 4.5, minutes: 90, tokens: 700_000 });
    expect(METRICS.map((metric) => metricValue(n, metric))).toEqual([2, 4.5, 90, 700_000]);
  });

  it("gives every metric its own mode, and every shade mode its metric back", () => {
    for (const metric of METRICS) {
      expect(metricOf(METRIC_SPECS[metric].mode)).toBe(metric);
    }
    expect(metricOf("idle")).toBeNull();
  });

  it("names each mode for its metric alone, so the four read as one family", () => {
    expect(METRICS.map((metric) => METRIC_SPECS[metric].label)).toEqual([
      "Retries",
      "Cost",
      "Time",
      "Tokens",
    ]);
  });

  it("puts a ticket with nothing run in the bottom band of every metric", () => {
    // Admitted means no `step_run` rows, so its three totals are zeroes rather than gaps — and
    // an untouched ticket must never shade as an expensive one.
    const fresh = node({ status: "running", runs: 0, attempt: null });
    for (const metric of METRICS) expect(bandOf(fresh, metric, DEFAULT_THRESHOLDS)).toBe(0);
  });
});

describe("rampBands", () => {
  it("labels each band with a value that genuinely lands in it", () => {
    // The legend is derived from the same two numbers `heatOf` splits on. This is the assertion
    // that keeps the key and the board from ever disagreeing.
    for (const metric of METRICS) {
      for (const entry of rampBands(metric, DEFAULT_THRESHOLDS)) {
        expect(heatOf(entry.probe, DEFAULT_THRESHOLDS[metric])).toBe(entry.band);
      }
    }
  });

  it("names the breaks in the metric's own units", () => {
    expect(rampBands("cost", DEFAULT_THRESHOLDS).map((entry) => entry.label)).toEqual([
      "Under $1.00",
      "$1.00 to $3.00",
      "$3.00 to $5.00",
      "$5.00+",
    ]);
    expect(rampBands("time", DEFAULT_THRESHOLDS)[3]?.label).toBe("8h+");
    expect(rampBands("tokens", DEFAULT_THRESHOLDS)[3]?.label).toBe("1.0M+");
  });
});

describe("lane vocabulary", () => {
  it("gives every lane a label and a hint, so none can render as its raw identifier", () => {
    for (const lane of LANES) {
      expect(LANE_LABELS[lane]).toBeTruthy();
      expect(LANE_HINTS[lane]).toBeTruthy();
    }
  });

  it("calls the retry lane Retrying", () => {
    expect(LANE_LABELS.retrying).toBe("Retrying");
  });
});

describe("a retry count", () => {
  it("is singular for one, the way a person would say it", () => {
    expect(METRIC_SPECS.retry.formatSum(1)).toBe("1 retry");
    expect(METRIC_SPECS.retry.formatSum(0)).toBe("0 retries");
    expect(METRIC_SPECS.retry.formatSum(3)).toBe("3 retries");
  });
});
