/**
 * The form family, after ADS: Form, FormFooter, Field, TextField, TextArea, Select, Checkbox,
 * RadioGroup, Toggle.
 *
 * Three rules every field keeps. Its id is generated unless the caller names one, so two fields
 * with one label never share an id. Its help and its error are linked to the control
 * (`aria-describedby`), and an error marks it `aria-invalid` — the red is never the only signal.
 * And a form's submit button stays pressable: `Form` checks on submit and puts the focus on the
 * first field that is wrong, rather than greying the button out with no reason given.
 *
 * Every control is the native element, styled — a select is a `<select>`, a checkbox a checkbox —
 * so keyboard, autofill and assistive technology behave as the browser already does. The text
 * controls are in `TextField.tsx`, the choices in `choices.tsx`.
 */

import { type FormEvent, type ReactNode, useId, useRef, useState } from "react";
import { cx } from "./cx";
import { Icon } from "./icons";

export type ControlProps = {
  id: string;
  "aria-describedby"?: string;
  "aria-invalid"?: true;
  required?: boolean;
};

export type FieldChrome = {
  label: string;
  id?: string;
  labelHidden?: boolean;
  isRequired?: boolean;
  helperMessage?: ReactNode;
  /** Why the value is refused. Shown in place of the helper, and marks the control invalid. */
  errorMessage?: ReactNode;
  validMessage?: ReactNode;
};

export function Field({
  label,
  id,
  labelHidden = false,
  isRequired = false,
  helperMessage,
  errorMessage,
  validMessage,
  className,
  children,
}: FieldChrome & { className?: string; children: (props: ControlProps) => ReactNode }) {
  const generated = useId();
  const controlId = id ?? generated;
  const messageId = `${controlId}-message`;
  const message = errorMessage || validMessage || helperMessage;
  return (
    <div className={cx("space-y-1", className)}>
      <label
        htmlFor={controlId}
        className={labelHidden ? "sr-only" : "block text-body-sm font-semibold text-subtle"}
      >
        {label}
        {isRequired && (
          <span aria-hidden="true" className="text-danger">
            {" "}
            *
          </span>
        )}
      </label>
      {children({
        id: controlId,
        "aria-describedby": message ? messageId : undefined,
        "aria-invalid": errorMessage ? true : undefined,
        required: isRequired || undefined,
      })}
      {message && (
        <p
          id={messageId}
          className={cx(
            "flex items-start gap-1 text-body-sm",
            errorMessage ? "text-danger" : validMessage ? "text-success" : "text-subtlest",
          )}
        >
          {errorMessage && <Icon glyph="status-error" size="small" className="mt-0.5" />}
          {validMessage && !errorMessage && (
            <Icon glyph="status-success" size="small" className="mt-0.5" />
          )}
          <span>{message}</span>
        </p>
      )}
    </div>
  );
}

/**
 * A form whose submit button stays pressable. On submit, `validate` answers whether the values
 * can go; if not, nothing is sent, `attempted` turns true so every field can show its error, and
 * the focus moves to the first field that says it is invalid.
 */
export function Form({
  onSubmit,
  validate,
  className,
  "aria-label": ariaLabel,
  children,
}: {
  /** Called with the form, for a field that is read off it rather than held in state. */
  onSubmit: (form: HTMLFormElement) => void;
  validate?: () => boolean;
  className?: string;
  "aria-label"?: string;
  children: ReactNode | ((state: { attempted: boolean }) => ReactNode);
}) {
  const [attempted, setAttempted] = useState(false);
  const form = useRef<HTMLFormElement>(null);
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const valid = !validate || validate();
    // Only a refused submit keeps the fields showing their errors: a form that clears itself
    // after a save must not turn red over the fields it just emptied.
    setAttempted(!valid);
    if (!valid) {
      requestAnimationFrame(() =>
        form.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus(),
      );
      return;
    }
    onSubmit(event.currentTarget);
  }
  return (
    <form ref={form} noValidate onSubmit={submit} aria-label={ariaLabel} className={className}>
      {typeof children === "function" ? children({ attempted }) : children}
    </form>
  );
}

/** The legend ADS forms carry when any field is required. */
export function RequiredLegend() {
  return (
    <p className="text-body-sm text-subtlest">
      Required fields are marked with an asterisk <span className="text-danger">*</span>
    </p>
  );
}

export function FormFooter({
  align = "start",
  children,
}: {
  align?: "start" | "end";
  children: ReactNode;
}) {
  return (
    <div
      className={cx(
        "flex flex-wrap items-center gap-2 pt-2",
        align === "end" ? "justify-end" : "justify-start",
      )}
    >
      {children}
    </div>
  );
}
