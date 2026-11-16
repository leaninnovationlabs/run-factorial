/**
 * A navigation row, after ADS's LinkItem and ButtonItem: an icon and a label, or — when the
 * navigation is compact — the icon alone, named by its label and by a tooltip beside it. A link
 * goes somewhere; a button opens something: a menu at the navigation's foot, say.
 *
 * A child row sits under a parent that groups it — a configuration page under Settings — indented,
 * any icon it has starting under the parent's label, and is not drawn when the navigation is
 * compact: the parent's icon stands for the group there. A parent given `expand` carries a chevron of its own beside the link
 * that shows or hides its children; the link still goes to the parent's page. A row's `trailing`
 * is a small fact at its right edge: a count, a state glyph.
 *
 * A button row can show something other than its label, and is then named by its label, which
 * says what pressing it does. The place the top bar
 * names is `NavPlace`: a link to its home and, beside it, the button of its menu.
 *
 * Selection is never the fill alone: a bar at the row's edge says "you are here" in shape as well.
 */

import type { ComponentPropsWithRef, ReactNode, Ref } from "react";
import { Link } from "react-router-dom";
import { cx } from "./cx";
import { type Glyph, Icon } from "./icons";
import { Tooltip } from "./Tooltip";

type Shared = { icon?: Glyph; label: string; isCompact?: boolean; isChild?: boolean };

function rowLook(
  isCompact: boolean,
  /** A child row, and whether it carries an icon. */
  child: "none" | "bare" | "icon",
  state: "current" | "open" | "rest",
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
  trailing,
  children,
  ...rest
}: Shared & {
  isOpen?: boolean;
  trailing?: ReactNode;
  children?: ReactNode;
} & Omit<ComponentPropsWithRef<"button">, "children" | "className" | "type">) {
  const button = (
    <button
      {...rest}
      type="button"
      aria-label={isCompact || children !== undefined ? label : undefined}
      className={rowLook(isCompact, childLook(isChild, icon), isOpen ? "open" : "rest")}
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

/**
 * The place the top bar names — the organization, the project — drawn as one button with two
 * parts: the name, a link to the place's home, and the chevron at its end, the button of the menu
 * that switches to another place. A hover fills the whole block, and the chevron's part a shade
 * deeper, so the pair reads as one control while the pointer still shows which part a press
 * reaches. Where there is no menu the block is the link alone.
 */
export function NavPlace({
  ref,
  isOpen = false,
  children,
}: {
  /** What the menu hangs from: the whole block, not the chevron alone. */
  ref?: Ref<HTMLDivElement>;
  isOpen?: boolean;
  children: ReactNode;
}) {
  return (
    <div
      ref={ref}
      className={cx(
        "group/place flex w-full min-w-0 items-stretch rounded-md transition-colors hover:bg-neutral-subtle-hovered",
        isOpen && "bg-neutral-subtle-hovered",
      )}
    >
      {children}
    </div>
  );
}

/** The name, cut short rather than wrapped; a tooltip says it in full when it is. */
export function NavPlaceLink({
  to,
  icon,
  name,
  pending,
  ...rest
}: {
  to: string;
  icon: Glyph;
  /** Undefined while it loads, when `pending` stands in its place. */
  name: string | undefined;
  pending?: ReactNode;
} & Omit<ComponentPropsWithRef<typeof Link>, "to" | "children" | "className">) {
  return (
    <Tooltip content={name ?? ""} position="bottom-start" onlyIfTruncated>
      <Link
        {...rest}
        to={to}
        className="focus-inset flex min-w-0 flex-1 items-center gap-2 rounded-md py-1.5 pr-2 pl-3 text-body font-semibold text-default"
      >
        <Icon glyph={icon} />
        <span className="min-w-0 flex-1 truncate">{name ?? pending}</span>
      </Link>
    </Tooltip>
  );
}

/** The chevron that opens the place's menu, named by what it does ("Switch project: widgets").
 *  Its fill sits over the block's, which is translucent, so it is always the deeper of the two. */
export function NavPlaceMenuButton({
  label,
  isOpen = false,
  ...rest
}: { label: string; isOpen?: boolean } & Omit<
  ComponentPropsWithRef<"button">,
  "children" | "className" | "type"
>) {
  return (
    <button
      {...rest}
      type="button"
      aria-label={label}
      className={cx(
        "focus-inset flex w-7 shrink-0 items-center justify-center rounded-r-md transition-colors",
        isOpen
          ? "bg-neutral-hovered text-default"
          : "text-icon-subtle group-hover/place:bg-neutral-subtle-pressed group-hover/place:text-default hover:bg-neutral-hovered",
      )}
    >
      <Icon glyph="chevron-down" size="small" />
    </button>
  );
}
