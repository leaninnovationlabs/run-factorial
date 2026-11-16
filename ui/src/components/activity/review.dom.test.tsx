// @vitest-environment happy-dom
/** The review's poll keeps the run's Files in step with the commit Approve would take. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../../api/client";
import { keys } from "../../api/queries";
import type { Review } from "../../api/types";
import { type Mounted, mount, settle } from "../../testDom";
import { useReviewOffer } from "./review";

const RUN = { id: 7, status: "interrupted" as const };
const FILES_STALE =
  "The branch moved after its files were read. Look again in a moment, then approve.";

function review(over: Partial<Review> = {}): Review {
  return {
    awaits_review: true,
    branch: "plan/12-billing",
    head_sha: "4f1c2a9e0b7d",
    pushed: true,
    approval: null,
    offered: true,
    refusal: null,
    ...over,
  };
}

function Owner() {
  useReviewOffer(RUN, true, { parkReason: "idle", poll: true });
  return null;
}

let view: Mounted | null = null;
afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.restoreAllMocks();
});

async function polled(first: Review) {
  vi.spyOn(api, "getReview").mockReturnValue(new Promise(() => {}));
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(keys.runReview(RUN.id), first);
  qc.setQueryData(keys.runOutputs(RUN.id), { step_run_id: RUN.id, entries: [] });
  view = await mount(
    <QueryClientProvider client={qc}>
      <Owner />
    </QueryClientProvider>,
  );
  return {
    next: async (r: Review) => {
      qc.setQueryData(keys.runReview(RUN.id), r);
      await settle();
    },
    filesInvalidated: () => qc.getQueryState(keys.runOutputs(RUN.id))?.isInvalidated,
  };
}

describe("the review poll", () => {
  it("reads the Files again when the branch moves", async () => {
    const poll = await polled(review());
    await poll.next(review({ pushed: false }));
    expect(poll.filesInvalidated()).toBe(false);
    await poll.next(review({ head_sha: "0b0b0b0b0b0b", refusal: FILES_STALE }));
    expect(poll.filesInvalidated()).toBe(true);
  });

  it("reads the Files again when the stale-files refusal clears at the same head", async () => {
    const poll = await polled(review({ refusal: FILES_STALE }));
    await poll.next(review({ refusal: FILES_STALE }));
    expect(poll.filesInvalidated()).toBe(false);
    await poll.next(review());
    expect(poll.filesInvalidated()).toBe(true);
  });
});
