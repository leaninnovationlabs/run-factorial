import { useRetryStep } from "../../api/queries";
import { formatAbsolute } from "../../format";
import { ConfirmPopup, useActionFeedback } from "../ui";
import type { RetryOffer } from "./retry";

/**
 * Start a failed step's next attempt now, from the failed attempt's own header, both while its
 * backoff runs and once it escalated (decision 77). An agent's attempt continues the failed
 * session where the runtime still can. It asks in a popup anchored to the button, as Exit does:
 * a press made by mistake spends one attempt, nothing more.
 */
export function RetryStep({ run, ticketId, due }: RetryOffer) {
  const retry = useRetryStep(ticketId);
  useActionFeedback(retry, { errorTitle: "Couldn't start the retry" });
  const next = run.attempt + 1;
  const starts =
    run.kind === "agent"
      ? `Attempt ${next} continues attempt ${run.attempt}'s session and folder, or starts fresh if it can't.`
      : `Attempt ${next} starts now.`;
  const when = due
    ? ` It starts now instead of at ${formatAbsolute(due)}.`
    : " One more try: if it fails, the ticket blocks again.";

  return (
    <ConfirmPopup
      label="Retry"
      name="Retry step"
      title={`Retry ${run.step_name}?`}
      confirmLabel="Retry step"
      consequence={starts + when}
      appearance="primary"
      spacing="compact"
      resetKey={`${run.id}-${run.status}`}
      isPending={retry.isPending}
      onConfirm={() => retry.mutate(run.id)}
    />
  );
}
