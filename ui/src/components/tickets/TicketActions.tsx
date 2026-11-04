import { type Ref, useState } from "react";
import {
  useCloseAndExitTicket,
  useExitTicket,
  useReenterTicket,
  type useRefreshTicket,
  useRequeueTicket,
  useTicketIssue,
  useUnblockTicket,
} from "../../api/queries";
import type { RequestResult, TicketDetail } from "../../api/types";
import { type ForgeHost, forgeName } from "../../forge";
import type { TicketCondition } from "../../status";
import { RedoStep } from "../activity/RedoStep";
import {
  ActionError,
  Button,
  ConfirmPopup,
  type ConfirmPopupHandle,
  useActionFeedback,
  WAITING_FOR_WORKER,
} from "../ui";
import { IssueActions } from "./IssueActions";
import { RestartDialog } from "./RestartDialog";
import { requestNote } from "./requestNote";

/**
 * The ticket's actions, in one row of small buttons, each a short word after its icon and named
 * in full for a screen reader. Written out rather than folded into a menu: a costly one confirms
 * in a popup anchored to its button (decision 50), and a menu would close before it could open.
 * Restart on latest cannot be undone, so it asks in a modal instead (`RestartDialog`).
 *
 * Each control is offered only where the backend would act on it — the rule that keeps a button
 * from promising what the loop will refuse — and where it is withheld, the reason is written
 * where it would have been: a disabled button shows no tooltip and leaves the tab order (#167).
 * Every request that fails says why beside the controls, and raises a flag that stays. What a
 * refresh is doing — waiting, checked, queued — is said at the end of the facts row instead
 * (`TicketPage`), which is why the page owns that request and its feedback.
 */
export function TicketActions({
  ticket,
  condition,
  repo,
  forge,
  failing,
  refresh,
  interruptNote,
  interruptError,
  requeueControl,
  retryOffered,
  redoTarget,
  redoControl,
  gateClosed = false,
  closeAndExitControl,
}: {
  ticket: TicketDetail;
  condition: TicketCondition | null;
  repo: string | undefined;
  forge?: ForgeHost;
  failing: boolean;
  refresh: ReturnType<typeof useRefreshTicket>;
  interruptNote: string | null;
  interruptError: unknown;
  requeueControl: Ref<ConfirmPopupHandle>;
  /** A Retry is offered on the failed step (`retryOffer`), which replaces Unblock there. */
  retryOffered: boolean;
  /** At a waiting gate, the step a redo takes (`TicketDetail.redo_target`); null elsewhere, where
   *  Redo is in the current step's block instead. */
  redoTarget: string | null;
  /** Lets the gate's message arm this Redo, so there is one confirmation. */
  redoControl: Ref<ConfirmPopupHandle>;
  /** The gate's pull request closed without a merge (decision 79): Close and exit is offered. */
  gateClosed?: boolean;
  /** Lets the gate's message arm Close and exit, so there is one confirmation. */
  closeAndExitControl?: Ref<ConfirmPopupHandle>;
}) {
  const exit = useExitTicket(ticket.id);
  const closeAndExit = useCloseAndExitTicket(ticket.id);
  const reenter = useReenterTicket(ticket.id);
  const requeue = useRequeueTicket(ticket.id);
  const unblock = useUnblockTicket(ticket.id);
  const [restarting, setRestarting] = useState(false);
  const [restarted, setRestarted] = useState<RequestResult>();
  const exited = condition === "exited";
  const issue = useTicketIssue(ticket.id, exited);
  const exitFeedback = useActionFeedback(exit, { errorTitle: "Couldn't exit the ticket" });
  const closeAndExitFeedback = useActionFeedback(closeAndExit, {
    errorTitle: "Couldn't close and exit the ticket",
  });
  const reenterFeedback = useActionFeedback(reenter, {
    errorTitle: "Couldn't re-enter the ticket",
  });
  const requeueFeedback = useActionFeedback(requeue, {
    errorTitle: "Couldn't requeue the ticket",
    successTitle: () => "Requeued. Work on the ticket resumes now.",
  });
  const unblockFeedback = useActionFeedback(unblock, {
    errorTitle: "Couldn't unblock the ticket",
  });
  const slow =
    exitFeedback.slow ||
    closeAndExitFeedback.slow ||
    reenterFeedback.slow ||
    requeueFeedback.slow ||
    unblockFeedback.slow;
  // Quarantined and done tickets are never leased (`acquire_leases`), so an exit, a re-entry, a
  // restart or a refresh filed on one would be recorded and never read.
  const live = condition !== null && condition !== "done" && condition !== "quarantined";
  // An escalation only: Resume releases an interrupted or waiting run in its kept workspace,
  // and the API refuses an un-block there.
  const escalated = condition === "blocked" || condition === "rejected";
  const retryInstead = condition === "blocked" && retryOffered;
  // Only once the lane has moved past the ticket's pin (#321): restarting on the pin it already
  // has is never this button's to offer. A retired lane has no head to restart on.
  const behind =
    ticket.lane_head != null && ticket.lane_head.process_version_id !== ticket.process_version_id;
  const notes = [
    requestNote("exit", exit.data),
    requestNote("close and exit", closeAndExit.data),
    requestNote("re-enter", reenter.data),
    requestNote("unblock", unblock.data),
    requestNote("restart", restarted),
    interruptNote,
  ].filter((note): note is string => note !== null);

  return (
    // biome-ignore lint/a11y/useSemanticElements: a row of actions, not a form's fieldset
    <div
      className="flex max-w-3xl flex-wrap items-center justify-end gap-2"
      role="group"
      aria-label="Ticket actions"
      data-assist-anchor="ticket.actions"
    >
      {live && !failing && (
        <Button
          spacing="compact"
          iconBefore="refresh"
          aria-label="Refresh ticket"
          isLoading={refresh.isPending}
          onClick={() => refresh.mutate()}
        >
          Refresh
        </Button>
      )}
      <IssueActions
        ticketId={ticket.id}
        externalKey={ticket.external_key}
        repo={repo}
        forge={forge}
        size="md"
      />
      {live && !exited && (
        <ConfirmPopup
          key={`exit-${condition}`}
          label="Exit"
          name="Exit ticket"
          title="Exit this ticket?"
          iconBefore="exclamation-circle"
          spacing="compact"
          confirmLabel="Exit ticket"
          consequence="Any running work stops and the ticket leaves the process. Use Re-enter to bring it back."
          resetKey={condition ?? "none"}
          isPending={exit.isPending}
          onConfirm={() => exit.mutate()}
        />
      )}
      {live && !exited && gateClosed && (
        <ConfirmPopup
          key={`close-exit-${condition}`}
          label="Close and exit"
          title="Close the issue and exit this ticket?"
          iconBefore="exclamation-circle"
          spacing="compact"
          confirmLabel="Close and exit"
          consequence={`This closes issue #${ticket.external_key} on ${forgeName(forge)} and exits the ticket. To bring it back, reopen the issue, then press Re-enter.`}
          resetKey={condition ?? "none"}
          isPending={closeAndExit.isPending}
          handle={closeAndExitControl}
          onConfirm={() => closeAndExit.mutate()}
        />
      )}
      {exited &&
        (issue.isPending ? (
          <span className="text-body-sm text-subtle">Checking the issue…</span>
        ) : issue.data?.state === "closed" ? null : (
          <ConfirmPopup
            key={`reenter-${condition}`}
            label="Re-enter"
            name="Re-enter ticket"
            title="Re-enter this ticket?"
            iconBefore="undo"
            spacing="compact"
            confirmLabel="Re-enter ticket"
            consequence="This starts a new attempt at the step where the ticket stopped, after a short wait."
            appearance="primary"
            resetKey={condition ?? "none"}
            isPending={reenter.isPending}
            onConfirm={() => reenter.mutate()}
          />
        ))}
      {live && behind && (
        <Button
          spacing="compact"
          iconBefore="arrow-up"
          aria-label="Restart ticket on the latest process"
          aria-haspopup="dialog"
          onClick={() => setRestarting(true)}
        >
          Restart on latest
        </Button>
      )}
      {condition === "quarantined" && (
        <ConfirmPopup
          key="requeue"
          label="Requeue"
          name="Requeue ticket"
          title="Requeue this ticket?"
          iconBefore="refresh"
          spacing="compact"
          confirmLabel="Requeue ticket"
          consequence="Factorial tries again. If the cause is still there, it stops again."
          appearance="primary"
          resetKey={condition}
          isPending={requeue.isPending}
          handle={requeueControl}
          onConfirm={() => requeue.mutate()}
        />
      )}
      {redoTarget && (
        <RedoStep
          ticketId={ticket.id}
          projectId={ticket.project_id}
          step={redoTarget}
          label={`Redo ${redoTarget}`}
          iconBefore="undo"
          resetKey={`${condition}-${redoTarget}`}
          handle={redoControl}
        />
      )}
      {retryInstead && (
        <span className="text-body-sm text-subtle">Retry is on the failed step</span>
      )}
      {escalated && !retryInstead && (
        <ConfirmPopup
          key={`unblock-${condition}`}
          label="Unblock"
          name="Unblock ticket"
          title="Unblock this ticket?"
          iconBefore="refresh"
          spacing="compact"
          confirmLabel="Unblock ticket"
          consequence={
            condition === "rejected"
              ? "This reopens the gate for a fresh review, as removing the factorial:blocked label would."
              : "This starts the blocked step again after a short wait, as removing the factorial:blocked label would."
          }
          appearance="primary"
          resetKey={condition ?? "none"}
          isPending={unblock.isPending}
          onConfirm={() => unblock.mutate()}
        />
      )}
      {slow && (
        <span role="status" className="basis-full text-right text-body-sm text-subtle">
          {WAITING_FOR_WORKER}
        </span>
      )}
      {notes.map((note) => (
        <span key={note} role="status" className="basis-full text-right text-body-sm text-subtle">
          {note}
        </span>
      ))}
      {[
        exit.error,
        closeAndExit.error,
        reenter.error,
        requeue.error,
        unblock.error,
        refresh.error,
        interruptError,
      ].map((failure, index) =>
        failure ? (
          // biome-ignore lint/suspicious/noArrayIndexKey: one slot per request, in a fixed order
          <span key={index} className="basis-full text-right">
            <ActionError error={failure} />
          </span>
        ) : null,
      )}
      {restarting && (
        <RestartDialog
          ticket={ticket}
          onClose={() => setRestarting(false)}
          onRestarted={(result) => {
            setRestarting(false);
            setRestarted(result);
          }}
        />
      )}
    </div>
  );
}
