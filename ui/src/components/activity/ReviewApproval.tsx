import { useId } from "react";
import { useApproveStep } from "../../api/queries";
import type { Review, StepRun } from "../../api/types";
import { shortSha } from "../../format";
import { Button, Code, ErrorMessage, ServerText, useActionFeedback } from "../ui";
import { useReviewOffer } from "./review";

/**
 * Approve a run of a step that awaits review, at the commit its branch ends on now: one line of
 * state and the button. A single press with nothing to confirm: the API refuses a press on a
 * commit the branch has moved past, and a person who approves too early answers the agent next.
 * Mount it only for a step that awaits review (`stepAwaitsReview`), where `useReviewOffer` says
 * it is `shown`.
 */
export function ReviewApproval({ run, ticketId }: { run: StepRun; ticketId: number }) {
  const { review, error } = useReviewOffer(run, true);
  const approve = useApproveStep(ticketId);
  useActionFeedback(approve, { errorTitle: "Couldn't approve the step" });
  if (!review) {
    return error ? <ErrorMessage error={error} title="Couldn't read what to approve" /> : null;
  }
  return (
    <ReviewApprovalView
      review={review}
      isPending={approve.isPending}
      onApprove={(headSha) => approve.mutate({ stepRunId: run.id, headSha })}
    />
  );
}

export function ReviewApprovalView({
  review,
  isPending,
  onApprove,
}: {
  review: Review;
  isPending: boolean;
  onApprove: (headSha: string) => void;
}) {
  const lineId = useId();
  if (!review.awaits_review || !review.offered) return null;
  const { approval, head_sha: head, branch } = review;
  const current = approval !== null && approval.head_sha === head;
  const moved = approval !== null && head !== null && !current;
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <p id={lineId} role="status" className="text-body-sm text-subtle">
        {current &&
          `Approved by ${approval.approved_by}. The ticket moves on when its other results are there. `}
        {moved && "The branch changed after the last approval. "}
        {review.refusal ? (
          <ServerText text={review.refusal} />
        ) : current ? (
          "An issue comment made after the approval holds the step until it is approved again or resumed."
        ) : (
          <>
            Approve this step to move the ticket on. Commit <Code>{shortSha(head)}</Code>
            {branch && (
              <>
                {" "}
                on <Code>{branch}</Code>
              </>
            )}
            .
          </>
        )}
      </p>
      <Button
        appearance="primary"
        isLoading={isPending}
        aria-disabled={review.refusal ? true : undefined}
        aria-describedby={review.refusal ? lineId : undefined}
        onClick={() => head && onApprove(head)}
      >
        Approve
      </Button>
    </div>
  );
}
