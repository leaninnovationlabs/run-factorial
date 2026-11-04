/**
 * The confirmation for a costly action that can be undone — exit, re-enter, requeue, activate
 * (decision 50). The first press opens a popup anchored to the button, over the page, saying what
 * the second press spends, with that action and Cancel; nothing on the page moves. The focus lands
 * on Cancel, the safe answer, and Cancel, Escape or a press outside closes it and hands the focus
 * back to the button.
 *
 * A popup rather than a dialog because the action is reversible: a modal would weigh a requeue
 * like a delete. Never inside a menu, which closes when anything in it is chosen.
 *
 * `resetKey` is what the confirmation belongs to: a new key — the ticket's condition changed under
 * a poll — closes one nobody gave for the new state. `handle` lets another control on the page
 * open it — a message whose remedy is this action — so there is still one confirmation, not two.
 * `children`, when given, sit under the consequence: an optional field the confirm sends along.
 * `onDismiss` runs on Cancel and Escape, the two deliberate ways back, so such a field starts
 * empty next time; a press outside or the button pressed again keeps what was typed.
 */

import { type ReactNode, type Ref, useEffect, useImperativeHandle, useRef, useState } from "react";
import { Button, type ButtonAppearance, type ButtonSpacing } from "./Button";
import { announce } from "./Flag";
import type { Glyph } from "./icons";
import { Popup } from "./Popup";

export type ConfirmPopupHandle = { arm: () => void };

export function ConfirmPopup({
  label,
  name,
  title,
  confirmLabel,
  consequence,
  appearance = "warning",
  isPending = false,
  disabledReason,
  resetKey,
  iconBefore,
  triggerAppearance = "default",
  spacing = "default",
  handle,
  onConfirm,
  onDismiss,
  children,
}: {
  /** What the button shows. */
  label: string;
  /** The button's name when its label is shorter than the action — "Exit" for "Exit ticket". It
   *  must contain the label, so a speech user can say what they see (WCAG 2.5.3). */
  name?: string;
  /** The question the popup asks, which is also its name: "Exit this ticket?". */
  title: string;
  confirmLabel: string;
  consequence: string;
  /** The confirm action's look: warning for a cost, danger for a loss, primary for a start. */
  appearance?: ButtonAppearance;
  isPending?: boolean;
  disabledReason?: string;
  resetKey: string;
  iconBefore?: Glyph;
  triggerAppearance?: ButtonAppearance;
  spacing?: ButtonSpacing;
  handle?: Ref<ConfirmPopupHandle>;
  onConfirm: () => void;
  /** Cancel or Escape. */
  onDismiss?: () => void;
  children?: ReactNode;
}) {
  const [armedFor, setArmedFor] = useState<string | null>(null);
  useImperativeHandle(handle, () => ({ arm: () => setArmedFor(resetKey) }), [resetKey]);
  const armed = armedFor === resetKey;
  const trigger = useRef<HTMLButtonElement | null>(null);
  const cancel = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (armed) announce(consequence);
  }, [armed, consequence]);

  function close() {
    setArmedFor(null);
    requestAnimationFrame(() => trigger.current?.focus());
  }
  function dismiss() {
    close();
    onDismiss?.();
  }

  return (
    <Popup
      isOpen={armed}
      onClose={(how) => {
        setArmedFor(null);
        if (how === "escape") onDismiss?.();
      }}
      role="dialog"
      label={title}
      placement="bottom-end"
      initialFocusRef={cancel}
      trigger={({ ref, ...aria }) => (
        <Button
          {...aria}
          ref={(node) => {
            trigger.current = node;
            if (typeof ref === "function") ref(node);
            else if (ref) ref.current = node;
          }}
          appearance={triggerAppearance}
          spacing={spacing}
          iconBefore={iconBefore}
          isSelected={armed ? true : undefined}
          isLoading={isPending}
          disabledReason={disabledReason}
          aria-label={name}
          onClick={() => (armed ? close() : setArmedFor(resetKey))}
        >
          {label}
        </Button>
      )}
    >
      <div className="grid w-80 max-w-full gap-3 p-4">
        <p className="text-heading-xs text-default">{title}</p>
        <p className="text-body text-subtle">{consequence}</p>
        {children}
        <div className="flex justify-end gap-2">
          <Button ref={cancel} appearance="subtle" spacing="compact" onClick={dismiss}>
            Cancel
          </Button>
          <Button
            appearance={appearance}
            spacing="compact"
            onClick={() => {
              close();
              onConfirm();
            }}
          >
            {confirmLabel}
          </Button>
        </div>
      </div>
    </Popup>
  );
}
