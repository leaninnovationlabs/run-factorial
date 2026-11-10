import { type ReactNode, useEffect, useId, useRef, useState } from "react";
import { useCompletionAnswer, useRequestCompletion } from "../../api/queries";
import type { CompletionResult } from "../../api/types";
import {
  Button,
  Checkbox,
  Code,
  ErrorMessage,
  ModalDialog,
  ServerText,
  Spinner,
  Tooltip,
  VisuallyHidden,
} from "../ui";
import type { ForceCompleteProps, NextNeeds } from "./force";
import { COMPLETION_REFUSALS } from "./words";

/**
 * Ask the loop to check a step's postconditions now and accept it when they hold (decision 78),
 * from the header of the attempt it would accept. A modal, not a popup: the answer arrives from
 * the loop a pass later, and the dialog waits for it. Opening it is the request, so it carries no
 * confirm of its own: a check that finds the conditions unmet changes nothing.
 *
 * When they do not hold, the person may continue with warning, having read every failure: the
 * second request names them, and the loop forces the step only when no other failure has
 * appeared since. A rework that has not moved the pull request its gate rejected is never forced.
 */
export function ForceComplete({ run, ticketId, withheld, next, onOpenChange }: ForceCompleteProps) {
  const [open, setOpen] = useState(false);
  const setOpenTo = (isOpen: boolean) => {
    setOpen(isOpen);
    onOpenChange?.(isOpen);
  };
  // Released on unmount too, or a page that outlives this button keeps holding its offer.
  const release = useRef<(() => void) | null>(null);
  release.current = open && onOpenChange ? () => onOpenChange(false) : null;
  useEffect(() => () => release.current?.(), []);
  const reasonId = useId();
  const button = (
    <Button
      appearance="default"
      spacing="compact"
      aria-disabled={withheld ? true : undefined}
      aria-describedby={withheld ? reasonId : undefined}
      onClick={() => setOpenTo(true)}
    >
      Force complete
    </Button>
  );
  return (
    <>
      {/* The reason on hover and focus, not beside the button as `disabledReason` writes it: the
          step header is already crowded at phone width. */}
      {withheld ? (
        <>
          <Tooltip content={withheld}>{button}</Tooltip>
          <VisuallyHidden id={reasonId}>{withheld}</VisuallyHidden>
        </>
      ) : (
        button
      )}
      {open && (
        <ForceCompleteDialog
          stepRunId={run.id}
          step={run.step_name}
          attempt={run.attempt}
          ticketId={ticketId}
          next={next ?? null}
          onClose={() => setOpenTo(false)}
        />
      )}
    </>
  );
}

function ForceCompleteDialog({
  stepRunId,
  step,
  attempt,
  ticketId,
  next,
  onClose,
}: {
  stepRunId: number;
  step: string;
  attempt: number;
  ticketId: number;
  next: NextNeeds | null;
  onClose: () => void;
}) {
  const request = useRequestCompletion(ticketId);
  const { mutate } = request;
  // Once per opening, whatever the effect does on a remount: a second request for the same run
  // only reads as `pending`, but it is a second wake of the loop for nothing.
  const asked = useRef(false);
  useEffect(() => {
    if (asked.current) return;
    asked.current = true;
    mutate({ stepRunId });
  }, [mutate, stepRunId]);
  const requestId = request.data?.request_id ?? null;
  const answer = useCompletionAnswer(stepRunId, requestId, request.data?.outcome === "pending");
  const result = answer.data ?? request.data;
  // Ticked for one answer only: a `changed` refusal is a new list, read afresh.
  const [checkedFor, setCheckedFor] = useState<number | null>(null);
  const checked = requestId !== null && checkedFor === requestId;
  const overridable = result?.outcome === "refused" && result.overridable;
  const close = useRef<HTMLButtonElement>(null);

  return (
    <ModalDialog
      title={`Force complete \`${step}\`?`}
      appearance="warning"
      initialFocusRef={close}
      onClose={onClose}
      footer={
        overridable ? (
          <>
            <Button ref={close} onClick={onClose}>
              Cancel
            </Button>
            <Button
              appearance="warning"
              aria-disabled={!checked}
              isLoading={request.isPending}
              onClick={() =>
                mutate({ stepRunId, allowUnmet: true, seenFailures: [...result.failures] })
              }
            >
              Continue with warning
            </Button>
          </>
        ) : (
          <Button ref={close} onClick={onClose}>
            Close
          </Button>
        )
      }
    >
      <ErrorMessage error={request.error ?? answer.error} />
      {result ? (
        <Answer result={result} attempt={attempt} next={next}>
          {overridable && (
            <Checkbox
              label="Continue anyway: I've checked these"
              isChecked={checked}
              onChange={(on) => setCheckedFor(on ? requestId : null)}
            />
          )}
        </Answer>
      ) : (
        !request.error && <Spinner inline label="Checking the expected results…" />
      )}
    </ModalDialog>
  );
}

function Answer({
  result,
  attempt,
  next,
  children,
}: {
  result: CompletionResult;
  attempt: number;
  next: NextNeeds | null;
  children?: ReactNode;
}) {
  if (result.outcome === "pending") {
    return <Spinner inline label="Checking the expected results…" />;
  }
  if (result.outcome === "accepted") {
    return (
      <p>Its expected results are met. Attempt {attempt} is accepted and the ticket moves on.</p>
    );
  }
  if (result.outcome === "forced") {
    return (
      <p>Forced complete. The ticket moves on, and its activity records that you forced it.</p>
    );
  }
  const reason = result.reason ?? "";
  if (result.failures.length === 0) {
    return <p>Not completed: {COMPLETION_REFUSALS[reason] ?? reason}.</p>;
  }
  return (
    <div className="space-y-3">
      <p>
        {reason === "unmet"
          ? "These aren't met:"
          : `Not completed: ${COMPLETION_REFUSALS[reason] ?? reason}. These aren't met:`}
      </p>
      <Failures items={result.failures} />
      {result.overridable && next && (
        <>
          <p>
            What <Code>{next.step}</Code> needs:
          </p>
          <Failures items={next.needs} />
        </>
      )}
      {children}
    </div>
  );
}

function Failures({ items }: { items: string[] }) {
  return (
    <ul className="list-disc space-y-1 pl-5">
      {items.map((item) => (
        <li key={item}>
          <ServerText text={item} />
        </li>
      ))}
    </ul>
  );
}
