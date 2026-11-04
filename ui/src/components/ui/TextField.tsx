/** The text controls of the form family: TextField, TextArea and Select (see `form.tsx`). */

import type {
  InputHTMLAttributes,
  ReactNode,
  Ref,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from "react";
import { cx } from "./cx";
import { type ControlProps, Field, type FieldChrome } from "./form";
import { Icon } from "./icons";

// 16px on a phone: iOS Safari zooms the page into any field set smaller when it takes the focus,
// and leaves it zoomed after.
const CONTROL =
  "rounded-md border border-input bg-input text-body max-md:text-body-lg text-default transition-colors placeholder:text-subtlest hover:bg-input-hovered aria-invalid:border-danger disabled:cursor-not-allowed disabled:border-disabled disabled:bg-disabled disabled:text-disabled";

const WIDTHS = { small: "w-24", medium: "w-48", large: "w-80", full: "w-full" } as const;

type TextFieldProps = Omit<InputHTMLAttributes<HTMLInputElement>, "width"> &
  Partial<FieldChrome> & {
    spacing?: "default" | "compact";
    isMonospaced?: boolean;
    isInvalid?: boolean;
    width?: keyof typeof WIDTHS;
    elemBeforeInput?: ReactNode;
    ref?: Ref<HTMLInputElement>;
  };

/** A text input. With `label` it brings its own `Field`; without one it goes inside a `Field`
 *  (or names itself with `aria-label`). */
export function TextField({
  label,
  id,
  labelHidden,
  isRequired,
  helperMessage,
  errorMessage,
  validMessage,
  spacing = "default",
  isMonospaced = false,
  isInvalid = false,
  width = "full",
  elemBeforeInput,
  className,
  ref,
  ...input
}: TextFieldProps) {
  const control = (props: Partial<ControlProps>) => {
    const field = (
      <input
        {...input}
        {...props}
        ref={ref}
        aria-invalid={props["aria-invalid"] ?? (isInvalid || undefined)}
        readOnly={input.readOnly}
        className={cx(
          CONTROL,
          spacing === "compact" ? "h-6 px-1.5" : "h-8 px-2",
          isMonospaced && "font-mono",
          input.readOnly && "bg-neutral hover:bg-neutral",
          elemBeforeInput ? "w-full pl-7" : WIDTHS[width],
          className,
        )}
      />
    );
    if (!elemBeforeInput) return field;
    return (
      <span className={cx("relative inline-flex items-center", WIDTHS[width])}>
        <span className="pointer-events-none absolute left-2 flex text-icon-subtle">
          {elemBeforeInput}
        </span>
        {field}
      </span>
    );
  };
  if (label === undefined) return control({ id });
  return (
    <Field
      label={label}
      id={id}
      labelHidden={labelHidden}
      isRequired={isRequired}
      helperMessage={helperMessage}
      errorMessage={errorMessage}
      validMessage={validMessage}
    >
      {control}
    </Field>
  );
}

type TextAreaProps = TextareaHTMLAttributes<HTMLTextAreaElement> &
  Partial<FieldChrome> & {
    isMonospaced?: boolean;
    isInvalid?: boolean;
    resize?: "none" | "vertical";
    minimumRows?: number;
    ref?: Ref<HTMLTextAreaElement>;
  };

export function TextArea({
  label,
  id,
  labelHidden,
  isRequired,
  helperMessage,
  errorMessage,
  validMessage,
  isMonospaced = false,
  isInvalid = false,
  resize = "vertical",
  minimumRows = 3,
  className,
  ref,
  ...area
}: TextAreaProps) {
  const control = (props: Partial<ControlProps>) => (
    <textarea
      rows={minimumRows}
      {...area}
      {...props}
      ref={ref}
      aria-invalid={props["aria-invalid"] ?? (isInvalid || undefined)}
      className={cx(
        CONTROL,
        "block w-full px-2 py-1.5 leading-relaxed",
        isMonospaced && "font-mono text-body-sm",
        resize === "none" ? "resize-none" : "resize-y",
        area.readOnly && "bg-neutral hover:bg-neutral",
        className,
      )}
    />
  );
  if (label === undefined) return control({ id });
  return (
    <Field
      label={label}
      id={id}
      labelHidden={labelHidden}
      isRequired={isRequired}
      helperMessage={helperMessage}
      errorMessage={errorMessage}
      validMessage={validMessage}
    >
      {control}
    </Field>
  );
}

export type SelectOption = { value: string; label: string; isDisabled?: boolean };

/** An option, or a named run of them drawn as one `<optgroup>`, as `DropdownMenu`'s groups are. */
export type SelectEntry = SelectOption | { group: string; options: SelectOption[] };

type SelectProps = Omit<SelectHTMLAttributes<HTMLSelectElement>, "children"> &
  Partial<FieldChrome> & {
    options: SelectEntry[];
    spacing?: "default" | "compact";
    width?: keyof typeof WIDTHS;
  };

function optionOf(option: SelectOption) {
  return (
    <option key={option.value} value={option.value} disabled={option.isDisabled}>
      {option.label}
    </option>
  );
}

/** A native select, dressed as a field; the chevron is drawn, the menu is the browser's own. */
export function Select({
  label,
  id,
  labelHidden,
  isRequired,
  helperMessage,
  errorMessage,
  validMessage,
  options,
  spacing = "default",
  width = "full",
  className,
  ...select
}: SelectProps) {
  const control = (props: Partial<ControlProps>) => (
    // A set width holds in a row of controls; only `full` gives way.
    <span
      className={cx(
        "relative inline-flex items-center",
        WIDTHS[width],
        width !== "full" && "shrink-0",
      )}
    >
      <select
        {...select}
        {...props}
        className={cx(
          CONTROL,
          "w-full appearance-none pr-7",
          spacing === "compact" ? "h-6 pl-1.5" : "h-8 pl-2",
          className,
        )}
      >
        {options.map((entry, at) =>
          "group" in entry ? (
            // By position: two groups may share a name.
            // biome-ignore lint/suspicious/noArrayIndexKey: the entries are the caller's, in order
            <optgroup key={at} label={entry.group}>
              {entry.options.map(optionOf)}
            </optgroup>
          ) : (
            optionOf(entry)
          ),
        )}
      </select>
      <Icon
        glyph="chevron-down"
        className="pointer-events-none absolute right-2 text-icon-subtle"
      />
    </span>
  );
  if (label === undefined) return control({ id });
  return (
    <Field
      label={label}
      id={id}
      labelHidden={labelHidden}
      isRequired={isRequired}
      helperMessage={helperMessage}
      errorMessage={errorMessage}
      validMessage={validMessage}
    >
      {control}
    </Field>
  );
}
