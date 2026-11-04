import { describe, expect, it } from "vitest";
import type { StepRun } from "../../api/types";
import { run } from "../../pages/TicketPage.fixture";
import { declaresFiles, gateReview, hasPreview, reviewTarget } from "./preview";

const at = (id: number, step: string, attempt: number, status: string): StepRun =>
  run(id, step, attempt, status) as StepRun;

/** `review` reruns `build`, which `test` follows: the sample's code_review_gate shape. */
const CONFIG = {
  entry: { step: "build" },
  steps: [
    { name: "build", kind: "agent" as const, next: "test", preview: { enabled: true } },
    { name: "test", kind: "agent" as const, next: "review" },
    {
      name: "review",
      kind: "human_gate" as const,
      next: "done",
      gate: { onReject: { rerunStep: "build" } },
    },
    { name: "docs", kind: "agent" as const, next: "done", preview: { enabled: false } },
    { name: "ci", kind: "check" as const, next: "done" },
  ],
  terminal: { step: "done" },
};

const WAITING = { current_step: "review", status: "waiting_gate" as const };

describe("hasPreview", () => {
  it("reads an enabled block and ignores a disabled or absent one", () => {
    expect(hasPreview(CONFIG, "build")).toBe(true);
    expect(hasPreview(CONFIG, "docs")).toBe(false);
    expect(hasPreview(CONFIG, "test")).toBe(false);
  });
});

describe("declaresFiles", () => {
  const files = { path: "specs/{ticket}-{slug}/spec.md", label: "Spec" };
  const config = {
    entry: { step: "plan" },
    steps: [
      { name: "plan", kind: "agent" as const, next: "spec", preview: { page: { path: "a.md" } } },
      { name: "spec", kind: "agent" as const, next: "build", preview: { files: [files] } },
      { name: "build", kind: "agent" as const, next: "docs", preview: { page: null, files: [] } },
      {
        name: "docs",
        kind: "agent" as const,
        next: "done",
        preview: { enabled: false, files: [files] },
      },
    ],
    terminal: { step: "done" },
  };

  it("counts a block that holds only a retired page as no preview at all", () => {
    expect(hasPreview(config, "plan")).toBe(false);
    expect(hasPreview(config, "spec")).toBe(true);
    expect(hasPreview(config, "build")).toBe(true);
  });

  it("reads a declared file, and ignores a retired page, a live-only or a disabled block", () => {
    expect(declaresFiles(config, "plan")).toBe(false);
    expect(declaresFiles(config, "spec")).toBe(true);
    expect(declaresFiles(config, "build")).toBe(false);
    expect(declaresFiles(config, "docs")).toBe(false);
    expect(declaresFiles(config, "absent")).toBe(false);
  });
});

describe("gateReview", () => {
  const built = at(1, "build", 1, "succeeded");
  const tested = at(2, "test", 1, "succeeded");
  const waiting = at(3, "review", 1, "waiting");

  it("offers the gate's rerun step, not the step just before the gate", () => {
    expect(gateReview(CONFIG, WAITING, [built, tested, waiting])).toEqual({
      gate: "review",
      run: built,
    });
  });

  it("needs no preview block on the rerun step", () => {
    const plain = {
      ...CONFIG,
      steps: CONFIG.steps.map((step) => {
        const { preview: _, ...rest } = step as typeof step & { preview?: unknown };
        return rest;
      }),
    };
    expect(gateReview(plain, WAITING, [built, tested, waiting])?.run).toBe(built);
  });

  it("offers nothing unless the ticket waits at a gate", () => {
    expect(gateReview(CONFIG, { current_step: "test", status: "running" }, [built])).toBeNull();
    expect(gateReview(null, WAITING, [built, tested, waiting])).toBeNull();
  });

  it("offers nothing when the rerun step is not an agent step", () => {
    const toCheck = {
      ...CONFIG,
      steps: CONFIG.steps.map((step) =>
        step.name === "review" ? { ...step, gate: { onReject: { rerunStep: "ci" } } } : step,
      ),
    };
    // A succeeded run of the check and a gate waiting after it: only the step's kind refuses.
    const checked = [built, tested, at(9, "ci", 1, "succeeded"), at(10, "review", 1, "waiting")];
    expect(gateReview(toCheck, WAITING, checked)).toBeNull();
  });

  it("offers nothing for a rerun attempt newer than the gate's waiting run", () => {
    const rebuilt = at(4, "build", 2, "succeeded");
    expect(gateReview(CONFIG, WAITING, [built, tested, waiting, rebuilt])).toBeNull();
  });

  it("offers nothing for a gate without onReject", () => {
    const noReject = {
      ...CONFIG,
      steps: CONFIG.steps.map((step) => (step.name === "review" ? { ...step, gate: {} } : step)),
    };
    expect(gateReview(noReject, WAITING, [built, tested, waiting])).toBeNull();
  });

  it("offers nothing once the gate decided, or before the rerun step succeeded", () => {
    expect(gateReview(CONFIG, WAITING, [built, tested, at(3, "review", 1, "rejected")])).toBeNull();
    const failed = at(4, "build", 2, "failed");
    expect(gateReview(CONFIG, WAITING, [built, waiting, failed])).toBeNull();
  });
});

describe("reviewTarget", () => {
  it("names the gate for the run under review and nothing for another attempt", () => {
    const first = at(1, "build", 1, "succeeded");
    const second = at(5, "build", 2, "succeeded");
    const review = { gate: "review", run: second };
    expect(reviewTarget(review, second)).toBe("review");
    expect(reviewTarget(review, first)).toBeNull();
    expect(reviewTarget(null, second)).toBeNull();
  });
});
