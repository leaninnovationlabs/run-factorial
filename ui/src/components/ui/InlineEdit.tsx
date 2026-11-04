/**
 * A value that is read as text and edited in place (ADS inline edit), for a field someone changes
 * now and then — a project's name, a runtime's name — instead of a form that is always open with a
 * Save button waiting for a change. Enter saves, Escape cancels, and a value that is refused says
 * why beside the field.
 */

import { type KeyboardEvent, useEffect, useId, useRef, useState } from "react";
import { errorText } from "../../format";
import { IconButton } from "./Button";
import { cx } from "./cx";
import { Field } from "./form";
import { Icon } from "./icons";
import { SpinnerGlyph } from "./SpinnerGlyph";

export function InlineEditText({
  label,
  value,
  placeholder = "None",
  validate,
  isLoading = false,
  error,
  isMonospaced = false,
  onConfirm,
}: {
  label: string;
  value: string;
  /** Shown, dimmed, when there is no value yet. */
  placeholder?: string;
  /** A message when the value cannot be saved, or null when it can. */
  validate?: (value: string) => string | null;
  isLoading?: boolean;
  /** The save's own failure, from the request. */
  error?: unknown;
  isMonospaced?: boolean;
  onConfirm: (value: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const [attempted, setAttempted] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const readView = useRef<HTMLButtonElement>(null);
  const labelId = useId();

  // `select()` alone does not move the focus in every browser, and the button that had it is gone.
  useEffect(() => {
    if (!editing) return;
    input.current?.focus();
    input.current?.select();
  }, [editing]);

  const problem = validate?.(draft) ?? null;

  function finish(save: boolean) {
    if (save) {
      setAttempted(true);
      if (problem) return;
      if (draft !== value) onConfirm(draft);
    }
    setEditing(false);
    setAttempted(false);
    requestAnimationFrame(() => readView.current?.focus());
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") {
      event.preventDefault();
      finish(true);
    } else if (event.key === "Escape") {
      // Spent here, so the drawer or dialog this sits in stays open.
      event.preventDefault();
      setDraft(value);
      finish(false);
    }
  }

  const saveError = error ? errorText(error) : null;

  if (!editing) {
    return (
      // `min-w-0`: in a grid or flex row, the unwrapped value would otherwise set this field's
      // smallest width and push it past its column instead of truncating.
      <div className="min-w-0 space-y-1">
        <p id={labelId} className="text-body-sm font-semibold text-subtle">
          {label}
        </p>
        <button
          ref={readView}
          type="button"
          aria-describedby={labelId}
          aria-label={`Edit ${label.toLowerCase()}: ${value || placeholder}`}
          onClick={() => {
            setDraft(value);
            setEditing(true);
          }}
          className={cx(
            "group -mx-1.5 flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-left text-body transition-colors hover:bg-neutral-subtle-hovered",
            isMonospaced && "font-mono",
          )}
        >
          <span className={cx("min-w-0 flex-1 truncate", value ? "text-default" : "text-subtlest")}>
            {value || placeholder}
          </span>
          {isLoading ? (
            <SpinnerGlyph size="small" />
          ) : (
            <Icon
              glyph="edit"
              size="small"
              className="text-icon-subtle opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100"
            />
          )}
        </button>
        {saveError && (
          <p role="alert" className="text-body-sm text-danger">
            {saveError}
          </p>
        )}
      </div>
    );
  }

  return (
    <Field label={label} errorMessage={attempted && problem ? problem : saveError}>
      {(props) => (
        <div className="flex items-center gap-1">
          <input
            {...props}
            ref={input}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
            className={cx(
              "h-8 min-w-0 flex-1 rounded-md border border-input bg-input px-2 text-body text-default aria-invalid:border-danger",
              isMonospaced && "font-mono",
            )}
          />
          <IconButton
            icon="check"
            label={`Save ${label.toLowerCase()}`}
            appearance="default"
            onClick={() => finish(true)}
          />
          <IconButton
            icon="close"
            label="Cancel"
            appearance="default"
            onClick={() => {
              setDraft(value);
              finish(false);
            }}
          />
        </div>
      )}
    </Field>
  );
}
