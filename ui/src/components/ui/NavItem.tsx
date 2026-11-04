/**
 * A side-navigation row, after ADS's LinkItem and ButtonItem: an icon and a label, or — when the
 * navigation is compact — the icon alone, named by its label and by a tooltip beside it. A link
 * goes somewhere; a button opens something, a menu at the navigation's foot.
 *
 * A child row sits under a parent that groups it — a configuration page under Settings — indented,
 * any icon it has starting under the parent's label, and is not drawn when the navigation is
 * compact: the parent's icon stands for the group there. A parent given `expand` carries a chevron of its own beside the link
 * that shows or hides its children; the link still goes to the parent's page. A row's `trailing`
 * is a small fact at its right edge: a count, a state glyph.
 *
 * A button row can show something other than its label — the project's name, the organization's
 * — and is then named by its label, which says what pressing it does ("Switch project: widgets").
 * A prominent row, the project at the head of its navigation, is set in the heading weight; the
 * band it sits on is the navigation's.
 *
 * Selection is never the fill alone: a bar at the row's edge says "you are here" in shape as well.
 */

import type { ComponentPropsWithRef, ReactNode } from "react";
import { Link } from "react-router-dom";
import { cx } from "./cx";
import { type Glyph, Icon } from "./icons";
import { Tooltip } from "./Tooltip";

type Shared = { icon?: Glyph; label: string; isCompact?: boolean; isChild?: boolean };

function rowLook(
  isCompact: boolean,
  /** A child row, and whether it carries an icon. */
  child: "none" | "bare" | "icon",
  state: "current" | "open" | "rest" | "prominent" | "prominent-open",
): string {
  return cx(
    "focus-inset relative flex items-center gap-2 rounded-md transition-colors",
    isCompact ? "size-10 justify-center" : "w-full py-1.5 pr-2 text-left",
    // A child's label lines up past its parent's: a bare child indents to 40px; one with an icon
    // starts that icon at 32px, just inside the parent's label, which begins at 36px.
    !isCompact &&
      (child === "icon"
        ? "pl-8 text-body-sm"
        : child === "bare"
          ? "pl-10 text-body-sm"
          : "pl-3 text-body"),
    state === "current" && "bg-selected font-medium text-selected",
    state === "open" && "bg-neutral-subtle-pressed text-default",
    state === "rest" && "text-subtle hover:bg-neutral-subtle-hovered hover:text-default",
    state === "prominent" && "font-semibold text-default hover:bg-neutral-subtle-hovered",
    state === "prominent-open" && "bg-neutral-subtle-pressed font-semibold text-default",
  );
}

const childLook = (isChild: boolean, icon: Glyph | undefined) =>
  isChild ? (icon ? "icon" : "bare") : "none";

export function NavLinkItem({
  to,
  icon,
  label,
  isCompact = false,
  isChild = false,
  isCurrent = false,
  trailing,
  expand,
  ...rest
}: Shared & {
  to: string;
  isCurrent?: boolean;
  trailing?: ReactNode;
  /** The row heads a group: whether the group shows, how to toggle it, and the group's id. */
  expand?: { isOpen: boolean; onToggle: () => void; controls: string };
} & Omit<ComponentPropsWithRef<typeof Link>, "to" | "children" | "className">) {
  if (isCompact && isChild) return null;
  const toggles = expand !== undefined && !isCompact;
  const link = (
    <Link
      {...rest}
      to={to}
      aria-current={isCurrent ? "page" : undefined}
      aria-label={isCompact ? label : undefined}
      className={cx(
        rowLook(isCompact, childLook(isChild, icon), isCurrent ? "current" : "rest"),
        // Room for the chevron, which sits over the row's right edge rather than inside the link.
        toggles && "pr-10",
      )}
    >
      <span
        aria-hidden="true"
        className={cx(
          "absolute inset-y-1 left-0 w-0.5 rounded-full",
          isCurrent && "bg-neutral-bold",
        )}
      />
      {icon && <Icon glyph={icon} />}
      {!isCompact && <span className="min-w-0 flex-1 truncate">{label}</span>}
      {!isCompact && trailing && <span className="flex shrink-0 items-center">{trailing}</span>}
    </Link>
  );
  if (isCompact) {
    return (
      <Tooltip content={label} position="right">
        {link}
      </Tooltip>
    );
  }
  if (!toggles) return link;
  return (
    <div className="relative">
      {link}
      <button
        type="button"
        aria-expanded={expand.isOpen}
        aria-controls={expand.controls}
        aria-label={`${expand.isOpen ? "Collapse" : "Expand"} ${label}`}
        onClick={expand.onToggle}
        className="focus-inset absolute inset-y-1 right-1 flex w-7 items-center justify-center rounded-sm text-icon-subtle hover:bg-neutral-subtle-hovered hover:text-default"
      >
        <Icon glyph={expand.isOpen ? "chevron-down" : "chevron-right"} />
      </button>
    </div>
  );
}

export function NavButtonItem({
  icon,
  label,
  isCompact = false,
  isChild = false,
  isOpen = false,
  isProminent = false,
  trailing,
  children,
  ...rest
}: Shared & {
  isOpen?: boolean;
  isProminent?: boolean;
  trailing?: ReactNode;
  children?: ReactNode;
} & Omit<ComponentPropsWithRef<"button">, "children" | "className" | "type">) {
  const button = (
    <button
      {...rest}
      type="button"
      aria-label={isCompact || children !== undefined ? label : undefined}
      className={rowLook(
        isCompact,
        childLook(isChild, icon),
        isProminent ? (isOpen ? "prominent-open" : "prominent") : isOpen ? "open" : "rest",
      )}
    >
      {icon && <Icon glyph={icon} />}
      {!isCompact && <span className="min-w-0 flex-1 truncate">{children ?? label}</span>}
      {!isCompact && trailing && <span className="flex shrink-0 items-center">{trailing}</span>}
    </button>
  );
  return isCompact ? (
    <Tooltip content={label} position="right">
      {button}
    </Tooltip>
  ) : (
    button
  );
}
