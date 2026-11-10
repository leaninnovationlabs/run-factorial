import { useId, useRef, useState } from "react";
import { useRestartPreview, useRestartTicket } from "../../api/queries";
import type { RequestResult, RestartPreview, RestartRun, TicketDetail } from "../../api/types";
import { shortSha } from "../../format";
import { Button, Checkbox, Code, ErrorMessage, ModalDialog, ServerText } from "../ui";

/**
 * The review behind Restart on latest (#321). It cannot be undone — the ticket's open pull
 * requests are closed and its branches deleted — so it is a modal (decision 50), after
 * `AdoptHeadDialog`. What it lists is the API's live read and only a forecast: the worker works
 * the footprint out again when it answers. Branch names are agent-chosen, so all of it is text.
 */
export function RestartDialog({
  ticket,
  onClose,
  onRestarted,
}: {
  ticket: Pick<TicketDetail, "id" | "project_id" | "external_key">;
  onClose: () => void;
  onRestarted: (result: RequestResult) => void;
}) {
  const preview = useRestartPreview(ticket.id, true);
  const restart = useRestartTicket(ticket.id, ticket.project_id);
  const cancel = useRef<HTMLButtonElement>(null);
  const target = preview.data?.target ?? null;
  const merged = preview.data?.merged_outside ?? [];
  const [acceptedMerged, setAcceptedMerged] = useState(false);
  const reason =
    blockedBy(preview.data, preview.isPending) ??
    (merged.length > 0 && !acceptedMerged
      ? "Confirm the merged work above to restart."
      : undefined);
  // In the body, not beside the button, and always rendered: a line that comes and goes moves
  // the footer with it.
  const reasonId = useId();

  return (
    <ModalDialog
      title={`Restart #${ticket.external_key} on the latest process?`}
      appearance="warning"
      width="medium"
      initialFocusRef={cancel}
      onClose={onClose}
      footer={
        <>
          <Button ref={cancel} onClick={onClose}>
            Cancel
          </Button>
          <Button
            appearance="warning"
            isLoading={restart.isPending}
            aria-disabled={reason !== undefined}
            aria-describedby={reason ? reasonId : undefined}
            onClick={() =>
              target &&
              restart.mutate(
                {
                  processVersionId: target.process_version_id,
                  allowMerged: merged.length > 0 && acceptedMerged,
                },
                { onSuccess: (result) => onRestarted(result) },
              )
            }
          >
            Restart ticket
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <ErrorMessage error={preview.error} />
        {preview.data && <Changes preview={preview.data} />}
        {merged.length > 0 && (
          <section className="space-y-2">
            <h3 className={HEADING}>Already merged</h3>
            <p className="text-body-sm">
              This ticket's work below is already merged into a branch it doesn't own. The restart
              starts again from scratch and may repeat it, unless the merge has since been undone.
            </p>
            <ul className="divide-y divide-default rounded border border-default">
              {merged.map((pr) => (
                <li key={pr.number} className="flex flex-wrap items-center gap-2 px-3 py-2">
                  <a
                    href={pr.url}
                    target="_blank"
                    rel="noreferrer"
                    className="font-medium hover:underline"
                  >
                    #{pr.number}
                  </a>
                  <span className="min-w-0 break-all text-body-sm text-subtle">
                    {pr.head} into {pr.base}
                  </span>
                </li>
              ))}
            </ul>
            <Checkbox
              label="Restart anyway: I've checked this merged work"
              isChecked={acceptedMerged}
              onChange={setAcceptedMerged}
            />
          </section>
        )}
        <p
          id={reasonId}
          aria-hidden={reason ? undefined : true}
          className={`text-body-sm text-subtle${reason ? "" : " invisible"}`}
        >
          <ServerText text={reason ?? "You can restart now."} />
        </p>
        <ErrorMessage error={restart.error} />
      </div>
    </ModalDialog>
  );
}

/** Why Restart ticket cannot be pressed now, or undefined when it can. */
function blockedBy(data: RestartPreview | undefined, loading: boolean): string | undefined {
  if (!data) {
    return loading
      ? "Loading what the restart would change"
      : "Couldn't load what the restart would change";
  }
  if (data.blockers.length > 0) return data.blockers.map((blocker) => blocker.detail).join(" ");
  if (data.target === null) return "This process was removed, so there is nothing to restart on.";
  if (data.current_pin) return "The ticket already uses the latest version of its process.";
  return undefined;
}

const HEADING = "text-heading-xxs text-subtle uppercase";

function Changes({ preview }: { preview: RestartPreview }) {
  const { live_run: live, parked_run: parked, close_prs: prs, delete_branches: branches } = preview;
  const removes = live !== null || parked !== null || prs.length > 0 || branches.length > 0;
  return (
    <>
      <p className="text-body">
        The ticket starts again at the first step of the latest version of its process. Nothing it
        did before carries over. Its history stays on this page.
      </p>
      <p className="text-body">
        From <Code>{shortSha(preview.current.sha)}</Code> to{" "}
        <Code>{shortSha(preview.target?.sha ?? null)}</Code>
      </p>
      {live && <RunSection heading="The run it cancels" run={live} />}
      {parked && <RunSection heading="The paused session this closes" run={parked} />}
      {prs.length > 0 && (
        <section className="space-y-2">
          <h3 className={HEADING}>Pull requests it closes</h3>
          <ul className="divide-y divide-default rounded border border-default">
            {prs.map((pr) => (
              <li key={pr.number} className="flex flex-wrap items-center gap-2 px-3 py-2">
                <a
                  href={pr.url}
                  target="_blank"
                  rel="noreferrer"
                  className="font-medium hover:underline"
                >
                  #{pr.number}
                </a>
                <span className="min-w-0 break-all text-body-sm text-subtle">
                  {pr.head} into {pr.base}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
      {branches.length > 0 && (
        <section className="space-y-2">
          <h3 className={HEADING}>Branches it deletes</h3>
          <ul className="divide-y divide-default rounded border border-default font-mono text-body-sm">
            {branches.map((branch) => (
              <li key={branch} className="px-3 py-2 break-all">
                {branch}
              </li>
            ))}
          </ul>
        </section>
      )}
      {!removes && (
        <p className="text-body-sm text-subtle">
          Nothing is running, and the ticket has no pull request or branch to remove.
        </p>
      )}
      <p className="text-body-sm text-subtle">
        You can't undo this. A closed pull request keeps its commits, and GitHub can restore a
        deleted branch.
      </p>
    </>
  );
}

function RunSection({ heading, run }: { heading: string; run: RestartRun }) {
  return (
    <section className="space-y-2">
      <h3 className={HEADING}>{heading}</h3>
      <p className="text-body">
        <Code>{run.step}</Code> attempt {run.attempt}
      </p>
    </section>
  );
}
