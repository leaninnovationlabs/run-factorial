/** Which runs read their review off the forge, and Approve drawn from what the API's review says,
 *  in each of the states it can be in. */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { Review, StepRunStatus } from "../../api/types";
import type { ProcessConfigLike, ProcessStepLike } from "../flow/processGraph";
import { ReviewApprovalView } from "./ReviewApproval";
import { readsReview, stepAwaitsReview } from "./review";

const HEAD = "4f1c2a9e0b7d3c5a";

function review(over: Partial<Review> = {}): Review {
  return {
    awaits_review: true,
    branch: "plan/12-billing",
    head_sha: HEAD,
    pushed: true,
    approval: null,
    offered: true,
    refusal: null,
    ...over,
  };
}

const at = (status: StepRunStatus) => ({ status });

const config = (
  postconditions: unknown,
  kind: ProcessStepLike["kind"] = "agent",
): ProcessConfigLike => ({
  entry: { step: "plan" },
  steps: [{ name: "plan", kind, next: "done", postconditions }],
  terminal: { step: "done" },
});

describe("stepAwaitsReview", () => {
  it("reads request_approval() anywhere in an agent step's postconditions", () => {
    expect(
      stepAwaitsReview(config({ all: ['file_exists("plan.md")', "request_approval()"] }), "plan"),
    ).toBe(true);
    expect(stepAwaitsReview(config("request_approval()"), "plan")).toBe(true);
  });

  it("is false for a step with other postconditions, none, no agent, or no such step", () => {
    expect(stepAwaitsReview(config({ all: ['pr_open("plan/{ticket}-*")'] }), "plan")).toBe(false);
    expect(stepAwaitsReview(config(undefined), "plan")).toBe(false);
    expect(stepAwaitsReview(config("request_approval()", "check"), "plan")).toBe(false);
    expect(stepAwaitsReview(config("request_approval()"), "build")).toBe(false);
    expect(stepAwaitsReview(null, "plan")).toBe(false);
  });
});

describe("readsReview", () => {
  it("reads an idle run, and a parked one unless its park is a person's stop or an exit", () => {
    expect(readsReview(at("idle"), true)).toBe(true);
    expect(readsReview(at("interrupted"), true)).toBe(true);
    for (const reason of ["idle", "ceiling", "rollover"] as const) {
      expect(readsReview(at("interrupted"), true, reason)).toBe(true);
    }
    for (const reason of ["user", "exited", null] as const) {
      expect(readsReview(at("interrupted"), true, reason)).toBe(false);
    }
  });

  it("never reads the forge for a step that does not await review, or a run not at its prompt", () => {
    expect(readsReview(at("idle"), false)).toBe(false);
    for (const status of ["running", "succeeded", "failed"] as const) {
      expect(readsReview(at(status), true)).toBe(false);
    }
    expect(readsReview(null, true)).toBe(false);
  });
});

function draw(r: Review): string {
  const html = renderToStaticMarkup(
    <ReviewApprovalView review={r} isPending={false} onApprove={() => {}} />,
  );
  return html.replace(/<[^>]+>/g, "");
}

describe("ReviewApproval", () => {
  it("offers the commit the branch ends on", () => {
    expect(draw(review())).toBe(
      "Approve this step to move the ticket on. Commit 4f1c2a9 on plan/12-billing.Approve",
    );
  });

  it("says why it is disabled, in the API's words", () => {
    const html = renderToStaticMarkup(
      <ReviewApprovalView
        review={review({ refusal: "Nothing is pushed yet." })}
        isPending={false}
        onApprove={() => {}}
      />,
    );
    expect(html).toContain("Nothing is pushed yet.");
    expect(html).toContain('aria-disabled="true"');
  });

  it("says who approved, and keeps Approve for a renewal, once the approval is of the head", () => {
    const approval = {
      approved_by: "dana@example.com",
      at: "2026-11-12T10:00:00Z",
      head_sha: HEAD,
    };
    expect(draw(review({ approval }))).toBe(
      "Approved by dana@example.com. The ticket moves on when its other results are there. An issue comment made after the approval holds the step until it is approved again or resumed.Approve",
    );
  });

  it("says the branch changed after an approval of an earlier commit, and offers Approve again", () => {
    const approval = {
      approved_by: "dana@example.com",
      at: "2026-11-12T10:00:00Z",
      head_sha: "0a0a0a0",
    };
    expect(draw(review({ approval }))).toBe(
      "The branch changed after the last approval. Approve this step to move the ticket on. Commit 4f1c2a9 on plan/12-billing.Approve",
    );
  });

  it("draws nothing for a run the API does not offer it on", () => {
    expect(draw(review({ offered: false }))).toBe("");
  });
});
