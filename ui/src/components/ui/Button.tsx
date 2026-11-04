/**
 * Button, LinkButton, IconButton and ButtonGroup, after ADS's button family (decision 50).
 *
 * The look is chosen by props — `appearance`, `spacing`, `isSelected`, `isLoading` — and each
 * combination is ONE complete class set. Nothing is appended and overridden, because Tailwind
 * orders utilities by its own sort, not by the order they are written: the padding overrides the
 * old button took never applied (`foundations.test.ts` counts any that come back). `className`
 * is for layout only.
 *
 * Disabled has two forms. `isDisabled` is for a control that cannot act and needs no reason. A
 * control that cannot act for a reason takes `disabledReason` instead: it stays focusable with
 * `aria-disabled`, ignores the press, and writes the reason beside itself, linked by
 * `aria-describedby` — because a disabled button leaves the tab order and no browser shows a
 * tooltip on one, which is where the reason used to live (#167). When the reason is already on
 * the page, pass `aria-disabled` and point `aria-describedby` at it; the button behaves the same.
 *
 * `outline` and `small` are not ADS's: they are for a toolbar over a framed file, where a subtle
 * button reads as plain text and a body-size label crowds the file's path.
 */

import { type ButtonHTMLAttributes, type MouseEvent, type ReactNode, type Ref, useId } from "react";
import { Link } from "react-router-dom";
import { cx } from "./cx";
import { type Glyph, Icon } from "./icons";
import { SpinnerGlyph } from "./SpinnerGlyph";
import { Tooltip } from "./Tooltip";

export type ButtonAppearance = "default" | "primary" | "subtle" | "outline" | "warning" | "danger";
export type ButtonSpacing = "default" | "compact" | "small";

const BASE =
  "relative inline-flex max-w-full shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-md border font-medium transition-colors select-none";

const SPACING: Record<ButtonSpacing, string> = {
  default: "h-8 px-3 text-body",
  compact: "h-6 px-2 text-body",
  small: "h-6 px-2 text-body-sm",
};

const SQUARE: Record<ButtonSpacing, string> = {
  default: "size-8 text-body",
  compact: "size-6 text-body",
  small: "size-6 text-body-sm",
};

const APPEARANCE: Record<ButtonAppearance, string> = {
  default: "border-bold bg-surface text-default hover:bg-surface-hovered active:bg-surface-pressed",
  primary:
    "border-transparent bg-neutral-bold text-inverse hover:bg-neutral-bold-hovered active:bg-neutral-bold-pressed",
  subtle:
    "border-transparent bg-neutral-subtle text-subtle hover:bg-neutral-subtle-hovered hover:text-default active:bg-neutral-subtle-pressed",
  outline:
    "border-bold bg-neutral-subtle text-default hover:bg-neutral-subtle-hovered active:bg-neutral-subtle-pressed",
  warning: "border-transparent bg-warning-bold text-warning-inverse hover:bg-warning-bold-hovered",
  danger: "border-transparent bg-danger-bold text-inverse hover:bg-danger-bold-hovered",
};

/** A pressed toggle: the finesse grey plus an edge in `border-selected`, never the fill alone. */
const SELECTED =
  "border-selected bg-selected text-selected hover:bg-selected-hovered active:bg-selected-pressed";

const DISABLED = "cursor-not-allowed border-transparent bg-disabled text-disabled";

/** A label's icon, at the label's size. */
const ICON_SIZE: Record<ButtonSpacing, "small" | "medium"> = {
  default: "medium",
  compact: "medium",
  small: "small",
};

type Look = {
  appearance?: ButtonAppearance;
  spacing?: ButtonSpacing;
  isSelected?: boolean;
  isDisabled?: boolean;
  shouldFitContainer?: boolean;
};

/** The classes for one state of the button. Exported for the rare control that has to BE a
 *  different element — a file input's label — and still read as a button. */
export function buttonLook({
  appearance = "default",
  spacing = "default",
  isSelected = false,
  isDisabled = false,
  shouldFitContainer = false,
  square = false,
}: Look & { square?: boolean }): string {
  return cx(
    BASE,
    square ? SQUARE[spacing] : SPACING[spacing],
    isDisabled ? DISABLED : isSelected ? SELECTED : APPEARANCE[appearance],
    shouldFitContainer && "w-full",
  );
}

type ButtonProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, "disabled"> &
  Look & {
    /** Shows a spinner in place of the label, keeps the width, and ignores presses. */
    isLoading?: boolean;
    /** Why the button cannot act, written beside it; the button stays focusable. */
    disabledReason?: string;
    iconBefore?: Glyph;
    iconAfter?: Glyph;
    ref?: Ref<HTMLButtonElement>;
  };

export function Button({
  appearance = "default",
  spacing = "default",
  isSelected,
  isDisabled = false,
  isLoading = false,
  disabledReason,
  shouldFitContainer,
  iconBefore,
  iconAfter,
  type = "button",
  className,
  children,
  onClick,
  ref,
  ...rest
}: ButtonProps) {
  const reasonId = useId();
  const unavailable =
    Boolean(disabledReason) || rest["aria-disabled"] === true || rest["aria-disabled"] === "true";
  const inert = unavailable || isLoading;
  const describedBy = cx(rest["aria-describedby"], disabledReason ? reasonId : undefined);
  const button = (
    <button
      {...rest}
      ref={ref}
      type={type}
      disabled={isDisabled}
      aria-disabled={unavailable || undefined}
      aria-busy={isLoading || undefined}
      // A selected page or place says so with `aria-current`, a disclosure with `aria-expanded`,
      // both of which the caller passes; a pressed toggle says so with `aria-pressed`. Never two.
      aria-pressed={
        isSelected === undefined ||
        rest["aria-current"] !== undefined ||
        rest["aria-expanded"] !== undefined
          ? rest["aria-pressed"]
          : isSelected
      }
      aria-describedby={describedBy || undefined}
      data-appearance={appearance}
      data-spacing={spacing}
      className={cx(
        buttonLook({
          appearance,
          spacing,
          isSelected,
          isDisabled: isDisabled || unavailable,
          shouldFitContainer,
        }),
        className,
      )}
      onClick={(event: MouseEvent<HTMLButtonElement>) => {
        if (inert) {
          event.preventDefault();
          return;
        }
        onClick?.(event);
      }}
    >
      <span className={cx("inline-flex min-w-0 items-center gap-1.5", isLoading && "invisible")}>
        {iconBefore && <Icon glyph={iconBefore} size={ICON_SIZE[spacing]} />}
        {children}
        {iconAfter && <Icon glyph={iconAfter} size={ICON_SIZE[spacing]} />}
      </span>
      {isLoading && (
        <span className="absolute inset-0 flex items-center justify-center">
          <SpinnerGlyph />
        </span>
      )}
    </button>
  );
  if (!disabledReason) return button;
  return (
    <>
      {button}
      <span id={reasonId} className="text-body-sm text-subtle">
        {disabledReason}
      </span>
    </>
  );
}

type LinkButtonProps = Look & {
  /** An internal route, followed by the router without a reload. */
  to?: string;
  /** An address outside the app; opens in a new tab. */
  href?: string;
  iconBefore?: Glyph;
  className?: string;
  children: ReactNode;
  "aria-label"?: string;
};

/** A navigation that looks like a button. Never a `<button>` inside a link: one control, one
 *  role, and cmd-click still opens a new tab. An external link ends with its icon, as ADS draws
 *  one, unless `iconBefore` already puts it first — in a row where every icon leads. */
export function LinkButton({
  to,
  href,
  appearance = "default",
  spacing = "default",
  shouldFitContainer,
  iconBefore,
  className,
  children,
  ...rest
}: LinkButtonProps) {
  const look = cx(buttonLook({ appearance, spacing, shouldFitContainer }), className);
  const inner = (
    <>
      {iconBefore && <Icon glyph={iconBefore} size={ICON_SIZE[spacing]} />}
      {children}
    </>
  );
  if (to !== undefined) {
    return (
      <Link to={to} className={look} data-appearance={appearance} data-spacing={spacing} {...rest}>
        {inner}
      </Link>
    );
  }
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className={look}
      data-appearance={appearance}
      data-spacing={spacing}
      {...rest}
    >
      {inner}
      {iconBefore !== "link-external" && <Icon glyph="link-external" size="small" />}
    </a>
  );
}

type IconButtonProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, "disabled" | "children"> & {
  icon: Glyph;
  /** Required: it is the button's only name, read out and shown in a tooltip. */
  label: string;
  appearance?: ButtonAppearance;
  spacing?: ButtonSpacing;
  shape?: "default" | "circle";
  isSelected?: boolean;
  isDisabled?: boolean;
  /** Shows a spinner in place of the icon and ignores presses, as `Button` does; so does
   *  `aria-disabled`, which looks disabled but keeps the focus `disabled` would drop. */
  isLoading?: boolean;
  ref?: Ref<HTMLButtonElement>;
};

export function IconButton({
  icon,
  label,
  appearance = "subtle",
  spacing = "default",
  shape = "default",
  isSelected,
  isDisabled = false,
  isLoading = false,
  type = "button",
  className,
  onClick,
  ref,
  ...rest
}: IconButtonProps) {
  const unavailable = rest["aria-disabled"] === true || rest["aria-disabled"] === "true";
  return (
    <Tooltip content={label}>
      <button
        {...rest}
        ref={ref}
        type={type}
        aria-label={label}
        disabled={isDisabled}
        aria-busy={isLoading || rest["aria-busy"]}
        // As `Button`: a disclosure or a place says so itself, never also as pressed.
        aria-pressed={
          rest["aria-current"] !== undefined || rest["aria-expanded"] !== undefined
            ? rest["aria-pressed"]
            : isSelected
        }
        onClick={(event: MouseEvent<HTMLButtonElement>) => {
          if (isLoading || unavailable) {
            event.preventDefault();
            return;
          }
          onClick?.(event);
        }}
        data-appearance={appearance}
        data-spacing={spacing}
        className={cx(
          buttonLook({
            appearance,
            spacing,
            isSelected,
            isDisabled: isDisabled || unavailable,
            square: true,
          }),
          shape === "circle" && "rounded-full",
          className,
        )}
      >
        {isLoading ? <SpinnerGlyph /> : <Icon glyph={icon} />}
      </button>
    </Tooltip>
  );
}

/** Related actions side by side, named as one group for a screen reader. */
export function ButtonGroup({ label, children }: { label?: string; children: ReactNode }) {
  return (
    // biome-ignore lint/a11y/useSemanticElements: a row of buttons, not a form's fieldset
    <div role="group" aria-label={label} className="inline-flex flex-wrap items-center gap-2">
      {children}
    </div>
  );
}
