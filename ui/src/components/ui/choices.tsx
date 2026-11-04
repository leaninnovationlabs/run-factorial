/** The choices of the form family: Checkbox, RadioGroup and Toggle (see `form.tsx`). */

import { type ReactNode, useId } from "react";
import type { Role } from "../../status";
import { cx } from "./cx";
import { Lozenge } from "./Lozenge";
import { SpinnerGlyph } from "./SpinnerGlyph";

/**
 * A checkbox and its label. `isIndeterminate` is a DOM property with no attribute, so it is set
 * on the element; `data-state` states the same fact where a server render can read it.
 */
export function Checkbox({
  label,
  labelHidden = false,
  isChecked,
  isIndeterminate = false,
  isDisabled = false,
  onChange,
  id,
  name,
  value,
}: {
  label: string;
  labelHidden?: boolean;
  isChecked: boolean;
  isIndeterminate?: boolean;
  isDisabled?: boolean;
  onChange: (checked: boolean) => void;
  id?: string;
  name?: string;
  value?: string;
}) {
  return (
    <label className="inline-flex items-center gap-2 text-body text-default">
      <input
        type="checkbox"
        id={id}
        name={name}
        value={value}
        ref={(element) => {
          if (element) element.indeterminate = isIndeterminate;
        }}
        data-state={isIndeterminate ? "partial" : isChecked ? "on" : "off"}
        checked={isChecked}
        disabled={isDisabled}
        onChange={(event) => onChange(event.target.checked)}
        className="size-3.5 shrink-0"
      />
      <span className={labelHidden ? "sr-only" : undefined}>{label}</span>
    </label>
  );
}

export type RadioOption = {
  value: string;
  label: string;
  description?: ReactNode;
  isDisabled?: boolean;
  /** Tile appearance only. */
  lozenge?: { text: string; appearance: Role };
  /** Tile appearance only: why the option cannot be chosen, under its description. */
  disabledReason?: string;
};

export function RadioGroup({
  label,
  name,
  options,
  value,
  onChange,
  isRequired = false,
  isInline = false,
  appearance = "list",
}: {
  label: string;
  name: string;
  options: RadioOption[];
  value: string;
  onChange: (value: string) => void;
  isRequired?: boolean;
  /** The options side by side: a few short ones, like Low, Medium and High. */
  isInline?: boolean;
  /** `tile`: each option a bordered card in a grid, for a choice its description decides. A tile
   *  group sits under a heading of its own, so its legend is for screen readers only. */
  appearance?: "list" | "tile";
}) {
  const tile = appearance === "tile";
  const Option = tile ? Tile : Row;
  return (
    <fieldset className={tile ? undefined : "space-y-2"}>
      <legend className={tile ? "sr-only" : "text-body-sm font-semibold text-subtle"}>
        {label}
        {isRequired && (
          <span aria-hidden="true" className="text-danger">
            {" "}
            *
          </span>
        )}
      </legend>
      <div
        className={
          tile
            ? "grid grid-cols-[repeat(auto-fit,minmax(14rem,1fr))] gap-3"
            : isInline
              ? "flex flex-wrap gap-x-6 gap-y-2"
              : "space-y-2"
        }
      >
        {options.map((option) => (
          <Option
            key={option.value}
            name={name}
            option={option}
            isChecked={value === option.value}
            isRequired={isRequired}
            onChange={onChange}
          />
        ))}
      </div>
    </fieldset>
  );
}

type OptionProps = {
  name: string;
  option: RadioOption;
  isChecked: boolean;
  isRequired: boolean;
  onChange: (value: string) => void;
};

function Row({ name, option, isChecked, isRequired, onChange }: OptionProps) {
  const descriptionId = option.description ? `${name}-${option.value}-description` : undefined;
  return (
    <label className="flex items-start gap-2 text-body text-default">
      <input
        type="radio"
        name={name}
        value={option.value}
        checked={isChecked}
        disabled={option.isDisabled}
        required={isRequired || undefined}
        aria-describedby={descriptionId}
        onChange={() => onChange(option.value)}
        className="mt-1 size-3.5 shrink-0"
      />
      <span className="space-y-0.5">
        <span className="block">{option.label}</span>
        {option.description && (
          <span id={descriptionId} className="block text-body-sm text-subtlest">
            {option.description}
          </span>
        )}
      </span>
    </label>
  );
}

/** A native radio inside its label, so the group keeps the browser's keyboard and reading. A
 *  chosen tile's border is 2px, and its padding gives the extra pixel back so nothing moves. */
function Tile({ name, option, isChecked, isRequired, onChange }: OptionProps) {
  const id = `${name}-${option.value}`;
  const descriptionId = option.description ? `${id}-description` : undefined;
  const reasonId = option.disabledReason ? `${id}-reason` : undefined;
  const disabled = option.isDisabled === true;
  return (
    <label
      className={cx(
        "flex items-start gap-2 rounded-md text-body",
        isChecked ? "border-2 border-brand bg-selected p-[11px]" : "border border-default p-3",
        disabled ? "cursor-not-allowed text-disabled" : "cursor-pointer text-default",
        !isChecked && (disabled ? "bg-disabled" : "bg-surface hover:bg-surface-hovered"),
      )}
    >
      <input
        type="radio"
        name={name}
        value={option.value}
        checked={isChecked}
        disabled={disabled}
        required={isRequired || undefined}
        aria-describedby={cx(descriptionId, reasonId) || undefined}
        onChange={() => onChange(option.value)}
        className="mt-1 size-3.5 shrink-0"
      />
      <span className="min-w-0 flex-1 space-y-1">
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="font-medium">{option.label}</span>
          {option.lozenge && (
            <Lozenge appearance={option.lozenge.appearance}>{option.lozenge.text}</Lozenge>
          )}
        </span>
        {option.description && (
          <span id={descriptionId} className="block text-body-sm text-subtle">
            {option.description}
          </span>
        )}
        {option.disabledReason && (
          <span id={reasonId} className="block text-body-sm text-subtlest">
            {option.disabledReason}
          </span>
        )}
      </span>
    </label>
  );
}

/** An on/off switch that applies at once — never a stand-in for a checkbox in a form. */
export function Toggle({
  label,
  labelHidden = false,
  isChecked,
  isLoading = false,
  isDisabled = false,
  onChange,
  id,
  describedBy,
}: {
  label: string;
  labelHidden?: boolean;
  isChecked: boolean;
  isLoading?: boolean;
  isDisabled?: boolean;
  onChange: (checked: boolean) => void;
  id?: string;
  /** Ids of the text beside the switch that a screen reader should read with it. */
  describedBy?: string;
}) {
  const generated = useId();
  const labelId = `${id ?? generated}-label`;
  return (
    <span className="inline-flex items-center gap-2 text-body text-default">
      <button
        type="button"
        id={id}
        role="switch"
        aria-checked={isChecked}
        aria-labelledby={labelId}
        aria-describedby={describedBy}
        aria-busy={isLoading || undefined}
        disabled={isDisabled}
        onClick={() => {
          if (!isLoading) onChange(!isChecked);
        }}
        className={cx(
          "relative inline-flex h-4 w-8 shrink-0 items-center rounded-full transition-colors disabled:cursor-not-allowed disabled:opacity-disabled",
          isChecked ? "bg-success-bold" : "bg-neutral-bold",
        )}
      >
        <span
          className={cx(
            "absolute inline-flex size-3 items-center justify-center rounded-full bg-surface text-subtle transition-transform",
            isChecked ? "translate-x-4.5" : "translate-x-0.5",
          )}
        >
          {isLoading && <SpinnerGlyph size="small" />}
        </span>
      </button>
      <span id={labelId} className={labelHidden ? "sr-only" : undefined}>
        {label}
      </span>
    </span>
  );
}
