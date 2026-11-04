import { type Ref, useState } from "react";
import { useRedoStep } from "../../api/queries";
import { requestNote } from "../tickets/requestNote";
import {
  ConfirmPopup,
  type ConfirmPopupHandle,
  type Glyph,
  TextArea,
  useActionFeedback,
} from "../ui";
import type { ButtonSpacing } from "../ui/Button";

/** Redo offered on the current step's block: its latest run, and what `RedoStep` needs. */
export interface RedoOffer {
  runId: number;
  step: string;
  ticketId: number;
  projectId: number;
}

/**
 * Throw away a step and every step after it, and run it again from a clean start (decision 76):
 * on the step the ticket is on, or at a waiting gate on the step it reviews. It cannot be undone,
 * yet it asks in a popup anchored to the button, as the owner chose over decision 50's modal: one
 * confirm, with an optional note the fresh agent receives as data. What the redo closed, deleted,
 * kept and left merged is on the timeline once the loop answers.
 */
export function RedoStep({
  ticketId,
  projectId,
  step,
  label,
  resetKey,
  handle,
  spacing = "compact",
  iconBefore,
}: {
  ticketId: number;
  projectId: number;
  /** The step a redo takes: `TicketDetail.redo_target`. */
  step: string;
  label: string;
  resetKey: string;
  handle?: Ref<ConfirmPopupHandle>;
  spacing?: ButtonSpacing;
  iconBefore?: Glyph;
}) {
  const redo = useRedoStep(ticketId, projectId);
  useActionFeedback(redo, { errorTitle: "Couldn't redo the step" });
  const [note, setNote] = useState("");
  const waiting = requestNote("redo", redo.data);

  return (
    <>
      <ConfirmPopup
        label={label}
        name={`Redo ${step}`}
        title={`Redo ${step}?`}
        confirmLabel={`Redo ${step}`}
        consequence={`Its pull requests close, its branches are deleted, and ${step} runs again fresh, with every step after it. Merged work stays merged.`}
        appearance="danger"
        spacing={spacing}
        iconBefore={iconBefore}
        resetKey={resetKey}
        isPending={redo.isPending}
        handle={handle}
        onConfirm={() => {
          const trimmed = note.trim();
          // Kept until the API accepts it: a refusal must not lose what the person typed.
          redo.mutate(
            { target_step: step, ...(trimmed ? { note: trimmed } : {}) },
            { onSuccess: () => setNote("") },
          );
        }}
        onDismiss={() => setNote("")}
      >
        <TextArea
          label="Note for the fresh agent (optional)"
          value={note}
          onChange={(event) => setNote(event.target.value)}
          maxLength={8000}
          minimumRows={2}
        />
      </ConfirmPopup>
      {waiting && (
        <span role="status" className="basis-full text-right text-body-sm text-subtle">
          {waiting}
        </span>
      )}
    </>
  );
}
