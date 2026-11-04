import { useTicketIssue } from "../../api/queries";
import type { TicketDetail } from "../../api/types";
import { type ForgeHost, forgeName, issueHref, pullHref, pullNoun } from "../../forge";
import type { TicketCondition } from "../../status";
import { Code, SectionMessage } from "../ui";

/**
 * What the ticket's state asks of the reader, each as one message with its way forward: a
 * quarantined ticket and how to requeue it, a ticket whose passes are failing, an exited ticket
 * whose issue is closed, a gate waiting on a pull request, with its step's preview and Request
 * changes, a gate held because its pull request closed, with Close and exit, and a session
 * waiting on an answer.
 */
export function TicketMessages({
  ticket,
  condition,
  failing,
  gatePr,
  gatePrHref,
  repo,
  forge,
  onRequeue,
  onOpenTerminal,
  review,
  gateClosed = false,
  onCloseAndExit,
}: {
  ticket: TicketDetail;
  condition: TicketCondition | null;
  failing: boolean;
  gatePr: string | null;
  /** When the API observed the MR/PR directly, use its URL instead of synthesizing one. */
  gatePrHref?: string | null;
  repo: string | undefined;
  forge?: ForgeHost;
  onRequeue: () => void;
  /** Opens the parked run's terminal, where Resume is. */
  onOpenTerminal?: () => void;
  /** What the waiting gate can be asked from here: its step's preview, sending the work back, and
   *  redoing the step it reviews (`redoStep`), which arms the header's Redo. */
  review?: {
    onOpenPreview?: () => void;
    onRequestChanges: () => void;
    onRedo?: () => void;
    redoStep?: string;
  } | null;
  /** The gate's pull request closed without a merge, so the gate holds (decision 79). */
  gateClosed?: boolean;
  /** Arms the header's Close and exit, so there is one confirmation. */
  onCloseAndExit?: () => void;
}) {
  const exited = condition === "exited";
  const host = forgeName(forge);
  const issue = useTicketIssue(ticket.id, exited);
  const messages = [];
  if (condition === "quarantined") {
    messages.push(
      <SectionMessage
        key="quarantined"
        appearance="error"
        title="Stopped after repeated errors"
        actions={[{ text: "Requeue ticket", onClick: onRequeue }]}
      >
        Factorial stopped working on this ticket after repeated errors. Find the latest error in
        Activity, fix it, then press Requeue ticket.
      </SectionMessage>,
    );
  } else if (failing && condition !== "done") {
    messages.push(
      <SectionMessage key="failing" appearance="warning" title="Couldn't check the ticket">
        Factorial couldn't read this ticket's latest state. It keeps trying, and you don't need to
        do anything yet.
      </SectionMessage>,
    );
  }
  if (exited && issue.data?.state === "closed") {
    const { repo: issueRepo, number } = issue.data;
    messages.push(
      <SectionMessage
        key="closed"
        appearance="information"
        title="The issue is closed"
        actions={[{ text: "Open issue", href: issueHref(issueRepo, String(number), forge) }]}
      >
        Issue #{number} is closed on {host}. Reopen it to re-enter.
      </SectionMessage>,
    );
  }
  if (condition === "waiting_gate") {
    const gateHref = gatePr ? gatePrHref || (repo ? pullHref(repo, gatePr, forge) : null) : null;
    const noun = pullNoun(forge) === "MR" ? "Merge request" : "Pull request";
    const view = gateHref
      ? [
          {
            text: forge?.kind === "gitlab" ? "View merge request" : "View pull request",
            href: gateHref,
          },
        ]
      : [];
    const redo =
      review?.onRedo && review.redoStep
        ? [{ text: `Redo ${review.redoStep}`, onClick: review.onRedo }]
        : [];
    if (gateClosed) {
      // No Request changes: a rework would continue on the branch of a closed pull request.
      const actions = [
        ...view,
        ...redo,
        ...(onCloseAndExit ? [{ text: "Close and exit", onClick: onCloseAndExit }] : []),
      ];
      messages.push(
        <SectionMessage
          key="gate"
          appearance="warning"
          title={`${noun} closed`}
          actions={actions.length > 0 ? actions : undefined}
        >
          {`${gatePr ? `${noun} #${gatePr}` : `The ${noun.toLowerCase()}`} was closed without merging, so the ticket is held at this gate. Reopen it on ${host} to continue the review, or close the issue and exit the ticket.`}
        </SectionMessage>,
      );
    } else {
      const actions = [
        ...view,
        ...(review?.onOpenPreview ? [{ text: "Open preview", onClick: review.onOpenPreview }] : []),
        ...(review ? [{ text: "Request changes", onClick: review.onRequestChanges }] : []),
        ...redo,
      ];
      messages.push(
        <SectionMessage
          key="gate"
          appearance="information"
          title="Waiting for a review"
          actions={actions.length > 0 ? actions : undefined}
        >
          {gatePr
            ? `${noun} #${gatePr} needs a person's decision before the ticket moves on.`
            : "This step needs a person's decision before the ticket moves on."}
        </SectionMessage>,
      );
    }
  }
  if (condition === "waiting_human") {
    messages.push(
      <SectionMessage
        key="waiting"
        appearance="warning"
        title="Waiting for you"
        actions={onOpenTerminal ? [{ text: "Open terminal", onClick: onOpenTerminal }] : undefined}
      >
        The agent waited for an answer and nobody answered, so its session paused. Answer in the
        terminal and press Resume session, or remove the <Code>factorial:waiting</Code> label from
        the issue.
      </SectionMessage>,
    );
  }
  if (messages.length === 0) return null;
  return <div className="space-y-2">{messages}</div>;
}
