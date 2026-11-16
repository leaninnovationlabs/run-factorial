/**
 * A popup anchored to the control that opens it — a menu, or a small dialog of explanation — and
 * the DropdownMenu built on it. The ContextMenu is the same menu at the point of a right-click,
 * and PopupAt the same small dialog at a point.
 *
 * A non-modal layer on the overlay stack (`layers.ts`): it takes Escape before whatever it sits
 * over, closes on a press outside it, and hands the focus back to its trigger. It does not make
 * the page inert — a menu is a moment, not a place — so it also closes when the focus leaves it.
 * Portalled only while open, so a server render never meets `createPortal`.
 */

import {
  type KeyboardEvent,
  type ReactNode,
  type Ref,
  type RefObject,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { type ButtonAppearance, type ButtonSpacing, buttonLook } from "./Button";
import { cx } from "./cx";
import { showFlag } from "./Flag";
import { type Box, type Placement, place } from "./floating";
import { type Glyph, Icon } from "./icons";
import { pushLayer } from "./layers";
import { Tooltip } from "./Tooltip";

export type PopupTriggerProps = {
  ref: Ref<HTMLButtonElement>;
  "aria-expanded": boolean;
  "aria-haspopup": "menu" | "dialog";
  "aria-controls"?: string;
};

/** How a popup closed: Escape, which hands the focus back to its trigger, or the person moving
 *  away from it, by a press outside or by the focus leaving. */
export type PopupClose = "escape" | "away";

/** What a DropdownMenu's own trigger must carry, spread onto the button that draws it. */
export type MenuTriggerProps = PopupTriggerProps & { onClick: () => void };

export function Popup({
  isOpen,
  onClose,
  trigger,
  placement = "bottom-start",
  role = "dialog",
  label,
  initialFocusRef,
  anchorRef,
  children,
}: {
  isOpen: boolean;
  onClose: (how: PopupClose) => void;
  trigger: (props: PopupTriggerProps) => ReactNode;
  placement?: Placement;
  role?: "dialog" | "menu";
  label: string;
  /** Where the focus lands on open; the popup itself when absent. */
  initialFocusRef?: RefObject<HTMLElement | null>;
  /** What the popup is placed against, when that is more than its trigger; the trigger still
   *  takes the focus back. */
  anchorRef?: RefObject<HTMLElement | null>;
  children: ReactNode;
}) {
  const id = useId();
  const button = useRef<HTMLButtonElement | null>(null);
  const anchor = useMemo<Anchor>(
    () => ({
      box: () => (anchorRef?.current ?? button.current)?.getBoundingClientRect() ?? null,
      focus: () => button.current?.focus(),
      contains: (node) => button.current?.contains(node) ?? false,
    }),
    [anchorRef],
  );
  return (
    <>
      {trigger({
        ref: button,
        "aria-expanded": isOpen,
        "aria-haspopup": role,
        "aria-controls": isOpen ? id : undefined,
      })}
      {isOpen &&
        typeof document !== "undefined" &&
        createPortal(
          <Surface
            id={id}
            role={role}
            label={label}
            anchor={anchor}
            placement={placement}
            initialFocusRef={initialFocusRef}
            onClose={onClose}
          >
            {children}
          </Surface>,
          document.body,
        )}
    </>
  );
}

/** What a surface is opened from: a button, or the point of a right-click. */
type Anchor = {
  /** What the surface is placed against; null while there is nothing to place against. */
  box: () => Box | null;
  /** Hands the focus back on Escape. */
  focus: () => void;
  /** Whether `node` is the anchor itself, so a press or a focus on it is not "away". */
  contains: (node: Node) => boolean;
};

function Surface({
  id,
  role,
  label,
  anchor,
  placement,
  initialFocusRef,
  onClose,
  children,
}: {
  id: string;
  role: "dialog" | "menu";
  label: string;
  anchor: Anchor;
  placement: Placement;
  initialFocusRef?: RefObject<HTMLElement | null>;
  onClose: (how: PopupClose) => void;
  children: ReactNode;
}) {
  const surface = useRef<HTMLDivElement>(null);
  const [at, setAt] = useState<{ top: number; left: number } | null>(null);
  const closer = useRef(onClose);
  useEffect(() => {
    closer.current = onClose;
  }, [onClose]);
  /** True while Escape hands the focus back: the surface losing it then is not a second close. A
   *  button anchor is recognised as the place the focus went; a point has no such element. */
  const handingBack = useRef(false);
  const close = useCallback(
    (how: PopupClose) => {
      if (handingBack.current) return;
      closer.current(how);
      if (how !== "escape") return;
      handingBack.current = true;
      anchor.focus();
      handingBack.current = false;
    },
    [anchor],
  );

  // Placed again whenever the button can have moved: the window never scrolls (`main` does, and
  // a scroll does not bubble, so it is caught on its way down), and a resize moves everything.
  useLayoutEffect(() => {
    const reposition = () => {
      const box = anchor.box();
      if (!box || !surface.current) return;
      setAt(
        place(
          box,
          surface.current.getBoundingClientRect(),
          { width: window.innerWidth, height: window.innerHeight },
          placement,
        ),
      );
    };
    reposition();
    document.addEventListener("scroll", reposition, true);
    window.addEventListener("resize", reposition);
    return () => {
      document.removeEventListener("scroll", reposition, true);
      window.removeEventListener("resize", reposition);
    };
  }, [anchor, placement]);

  useEffect(() => {
    const element = surface.current;
    if (!element) return;
    const pop = pushLayer(element, () => close("escape"), { modal: false });
    // A field inside that took the focus as it mounted keeps it.
    if (!element.contains(document.activeElement)) (initialFocusRef?.current ?? element).focus();
    const onPointer = (event: PointerEvent) => {
      const target = event.target as Node;
      if (element.contains(target) || anchor.contains(target)) return;
      close("away");
    };
    document.addEventListener("pointerdown", onPointer);
    return () => {
      pop();
      document.removeEventListener("pointerdown", onPointer);
    };
  }, [anchor, close, initialFocusRef]);

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: its role is dialog or menu, passed in
    // biome-ignore lint/a11y/useAriaPropsSupportedByRole: its role is dialog or menu, passed in
    <div
      ref={surface}
      id={id}
      role={role}
      aria-label={label}
      tabIndex={-1}
      style={at ? { top: at.top, left: at.left } : { top: -9999, left: -9999 }}
      onBlur={(event) => {
        const next = event.relatedTarget as Node | null;
        if (next && !surface.current?.contains(next) && !anchor.contains(next)) {
          close("away");
        }
      }}
      className="fixed z-popup max-w-sm min-w-48 rounded-lg bg-surface-overlay text-body text-default shadow-overlay"
    >
      {children}
    </div>
  );
}

export type MenuItem = {
  label: string;
  onClick: () => void;
  /** Drawn before the label: what kind of thing the item makes or acts on. */
  icon?: Glyph;
  description?: string;
  /** Why the item cannot run now. It stays in the menu and pressable, says so beneath its label,
   *  and choosing it explains instead of acting (ADS: avoid disabled menu items). */
  unavailable?: string;
  /** One of a set only one of which holds — a theme — read out as a checked radio item. */
  isSelected?: boolean;
};

/** An item that opens a menu of its own beside it: Settings, say, holding Theme. */
export type MenuSubmenu = { label: string; submenu: MenuEntry[] };

/** A few choices as icons on one row, one of which holds — the theme — each named by a tooltip
 *  and read out as a checked radio item, so it is still one level of the menu. */
export type MenuChoices = {
  label: string;
  choices: { label: string; glyph: Glyph; isSelected: boolean; onClick: () => void }[];
};

export type MenuEntry = MenuItem | MenuSubmenu | MenuChoices | { group: string; items: MenuItem[] };

const ITEM =
  "focus-inset flex w-full items-start gap-2 px-3 py-1.5 text-left text-body text-default hover:bg-neutral-subtle-hovered active:bg-neutral-subtle-pressed";

/**
 * A button that opens a menu, with the ARIA menu-button pattern: the focus moves to the first
 * item, the arrow keys (and Home and End) move between items, Enter or a click chooses one, and
 * Escape or Tab closes the menu — Escape handing the focus back to the button. An item with a
 * submenu opens it on a hover, a click, Enter or the right arrow; the left arrow or Escape closes
 * just that submenu, back on the item that opened it.
 */
export function DropdownMenu({
  trigger,
  items,
  heading,
  placement = "bottom-end",
  anchorRef,
}: {
  trigger: {
    label: string;
    icon?: Glyph;
    /** The label is the button's name only, read out and shown in no text. */
    iconOnly?: boolean;
    appearance?: ButtonAppearance;
    spacing?: ButtonSpacing;
    /** Draws the control itself — a navigation row, say — from what makes it this menu's button;
     *  the rest of the spec but `label` is then unused. */
    render?: (props: MenuTriggerProps, state: { isOpen: boolean }) => ReactNode;
  };
  items: MenuEntry[];
  /** What the menu is about, written above its items: who is signed in, say. */
  heading?: string;
  placement?: Placement;
  /** What the menu hangs from, when that is more than its button (`Popup`). */
  anchorRef?: RefObject<HTMLElement | null>;
}) {
  const [open, setOpen] = useState(false);
  const first = useRef<HTMLButtonElement | null>(null);
  const button = useRef<HTMLButtonElement | null>(null);

  function choose(item: MenuItem) {
    // Back on the button before acting: the item is about to unmount, and a dialog the item opens
    // records whatever has the focus as the place to return it to.
    button.current?.focus();
    setOpen(false);
    if (item.unavailable) {
      showFlag({ appearance: "info", title: item.unavailable });
      return;
    }
    item.onClick();
  }

  return (
    <Popup
      isOpen={open}
      onClose={() => setOpen(false)}
      role="menu"
      label={trigger.label}
      placement={placement}
      initialFocusRef={first}
      anchorRef={anchorRef}
      trigger={({ ref, ...aria }) => {
        const props: MenuTriggerProps = {
          ...aria,
          ref: (node) => {
            button.current = node;
            if (typeof ref === "function") ref(node);
            else if (ref) ref.current = node;
          },
          onClick: () => setOpen((value) => !value),
        };
        if (trigger.render) return trigger.render(props, { isOpen: open });
        const control = (
          <button
            {...props}
            type="button"
            aria-label={trigger.iconOnly ? trigger.label : undefined}
            className={buttonLook({
              appearance: trigger.appearance ?? "default",
              spacing: trigger.spacing ?? "default",
              isSelected: open,
              square: trigger.iconOnly,
            })}
          >
            {trigger.icon && <Icon glyph={trigger.icon} />}
            {!trigger.iconOnly && trigger.label}
            {!trigger.iconOnly && <Icon glyph="chevron-down" size="small" />}
          </button>
        );
        // An icon alone is named in words the moment a pointer or the keyboard reaches it.
        return trigger.iconOnly ? <Tooltip content={trigger.label}>{control}</Tooltip> : control;
      }}
    >
      {/* biome-ignore lint/a11y/noStaticElementInteractions: Tab leaves the menu from its button */}
      <div
        onKeyDown={(event) => {
          if (event.key !== "Tab") return;
          // From the button, so the browser's own Tab moves on from where the menu was opened
          // rather than from the end of the page, where the menu is portalled.
          button.current?.focus();
          setOpen(false);
        }}
      >
        {heading && (
          <p
            role="none"
            className="border-b border-default px-3 pt-2 pb-2 text-body-sm text-subtle"
          >
            {heading}
          </p>
        )}
        <MenuList entries={items} onChoose={choose} firstRef={first} />
      </div>
    </Popup>
  );
}

/** The anchor of something opened at a point in the window instead of from a button. Keyed on
 *  the point's numbers, not on the object that carries them, so a caller may build it inline. */
function usePointAnchor(at: { x: number; y: number } | null): Anchor | null {
  const x = at?.x;
  const y = at?.y;
  return useMemo<Anchor | null>(() => {
    if (x === undefined || y === undefined || typeof document === "undefined") return null;
    // What had the focus as it opened, read before the surface takes it.
    const opener = document.activeElement as HTMLElement | null;
    return {
      box: () => ({ top: y, left: x, width: 0, height: 0 }),
      focus: () => opener?.focus?.(),
      contains: () => false,
    };
  }, [x, y]);
}

/**
 * A menu at the point of a right-click: `DropdownMenu`'s surface, layer and list, placed where
 * the pointer was instead of under a button. A right-click has no keyboard or touch equivalent,
 * so the same entries are also behind a button on the row that opens this
 * (docs/design-system.md, Patterns).
 */
export function ContextMenu({
  at,
  label,
  items,
  onClose,
}: {
  /** The pointer's place in the window (`clientX`, `clientY`); null is closed. */
  at: { x: number; y: number } | null;
  label: string;
  items: MenuEntry[];
  onClose: () => void;
}) {
  const id = useId();
  const first = useRef<HTMLButtonElement | null>(null);
  const anchor = usePointAnchor(at);
  if (!anchor) return null;

  function choose(item: MenuItem) {
    // The same order as `DropdownMenu`: the focus goes back before the item acts, so a dialog
    // the item opens records the right place to return it to.
    anchor?.focus();
    onClose();
    if (item.unavailable) {
      showFlag({ appearance: "info", title: item.unavailable });
      return;
    }
    item.onClick();
  }

  return createPortal(
    <Surface
      id={id}
      role="menu"
      label={label}
      anchor={anchor}
      placement="bottom-start"
      initialFocusRef={first}
      onClose={onClose}
    >
      {/* biome-ignore lint/a11y/noStaticElementInteractions: Tab leaves the menu to where it was opened */}
      <div
        onKeyDown={(event) => {
          if (event.key !== "Tab") return;
          anchor.focus();
          onClose();
        }}
      >
        <MenuList entries={items} onChoose={choose} firstRef={first} />
      </div>
    </Surface>,
    document.body,
  );
}

/**
 * A small dialog at a point in the window: the popup's surface and layer with no button of its
 * own to hang from, for a question that belongs where the person just pressed, such as the name
 * of the thing they chose to add. Escape, a press outside it or the focus leaving closes it.
 */
export function PopupAt({
  at,
  label,
  onClose,
  children,
}: {
  /** A place in the window (`clientX`, `clientY`); null is closed. */
  at: { x: number; y: number } | null;
  label: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const id = useId();
  const anchor = usePointAnchor(at);
  if (!anchor) return null;
  return createPortal(
    <Surface
      id={id}
      role="dialog"
      label={label}
      anchor={anchor}
      placement="bottom-start"
      onClose={onClose}
    >
      {children}
    </Surface>,
    document.body,
  );
}

/** A list's own items, leaving out those of a submenu open inside it. */
function itemsOf(list: HTMLElement | null): HTMLButtonElement[] {
  return [...(list?.querySelectorAll<HTMLButtonElement>('[role^="menuitem"]') ?? [])].filter(
    (item) => item.closest("[data-menu-list]") === list,
  );
}

/**
 * One level of a menu. It answers only the keys pressed on its own items, so a submenu, which
 * sits inside it in the DOM, moves its own focus without its parent moving it again.
 */
function MenuList({
  entries,
  onChoose,
  firstRef,
  onLeave,
  autoFocus = false,
}: {
  entries: MenuEntry[];
  onChoose: (item: MenuItem) => void;
  firstRef?: RefObject<HTMLButtonElement | null>;
  /** Closes this list when it is a submenu, handing the focus back to the item that opened it. */
  onLeave?: () => void;
  autoFocus?: boolean;
}) {
  const own = useRef<HTMLDivElement>(null);
  const openers = useRef(new Map<string, HTMLButtonElement>());
  const [sub, setSub] = useState<{ label: string; focus: boolean } | null>(null);
  const firstEntry = entries[0] && "group" in entries[0] ? entries[0].items[0] : entries[0];

  const ownItems = () => itemsOf(own.current);

  useEffect(() => {
    if (autoFocus) itemsOf(own.current)[0]?.focus();
  }, [autoFocus]);

  function closeSub() {
    const opener = sub && openers.current.get(sub.label);
    setSub(null);
    opener?.focus();
  }

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if ((event.target as Element).closest("[data-menu-list]") !== own.current) return;
    if (onLeave && (event.key === "ArrowLeft" || event.key === "Escape")) {
      // preventDefault keeps the layer stack from closing the whole menu on this Escape.
      event.preventDefault();
      onLeave();
      return;
    }
    const all = ownItems();
    const current = all.indexOf(document.activeElement as HTMLButtonElement);
    if (event.key === "ArrowRight") {
      const label = all[current]?.dataset.submenu;
      if (label === undefined) return;
      event.preventDefault();
      setSub({ label, focus: true });
      return;
    }
    const next =
      event.key === "ArrowDown"
        ? (current + 1) % all.length
        : event.key === "ArrowUp"
          ? (current - 1 + all.length) % all.length
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? all.length - 1
              : -1;
    if (next === -1) return;
    event.preventDefault();
    all[next]?.focus();
  }

  const renderChoices = (entry: MenuChoices) => {
    const ref = entry === firstEntry ? firstRef : undefined;
    return (
      // biome-ignore lint/a11y/useSemanticElements: a group of menu items, as ARIA spells it
      <div
        key={entry.label}
        role="group"
        aria-label={entry.label}
        className="flex items-center justify-between gap-4 border-b border-default px-3 py-1.5 last:border-b-0"
      >
        <span aria-hidden="true" className="text-body text-default">
          {entry.label}
        </span>
        <span className="flex items-center gap-0.5">
          {entry.choices.map((choice, at) => (
            <Tooltip key={choice.label} content={choice.label}>
              <button
                ref={at === 0 ? ref : undefined}
                type="button"
                role="menuitemradio"
                aria-checked={choice.isSelected}
                aria-label={choice.label}
                onPointerEnter={() => setSub(null)}
                onClick={() => onChoose({ label: choice.label, onClick: choice.onClick })}
                className={cx(
                  "focus-inset flex size-7 items-center justify-center rounded-md transition-colors",
                  choice.isSelected
                    ? "bg-selected text-selected"
                    : "text-subtle hover:bg-neutral-subtle-hovered hover:text-default",
                )}
              >
                <Icon glyph={choice.glyph} />
              </button>
            </Tooltip>
          ))}
        </span>
      </div>
    );
  };

  const renderItem = (entry: MenuItem | MenuSubmenu | MenuChoices) => {
    if ("choices" in entry) return renderChoices(entry);
    const ref = entry === firstEntry ? firstRef : undefined;
    if ("submenu" in entry) {
      const isOpen = sub?.label === entry.label;
      return (
        <div key={entry.label}>
          <button
            ref={(node) => {
              if (node) openers.current.set(entry.label, node);
              else openers.current.delete(entry.label);
              if (ref) ref.current = node;
            }}
            type="button"
            role="menuitem"
            aria-haspopup="menu"
            aria-expanded={isOpen}
            data-submenu={entry.label}
            onPointerEnter={() => setSub({ label: entry.label, focus: false })}
            onClick={() => setSub({ label: entry.label, focus: true })}
            className={cx(
              ITEM,
              "items-center justify-between",
              isOpen && "bg-neutral-subtle-hovered",
            )}
          >
            <span>{entry.label}</span>
            <Icon glyph="chevron-right" size="small" className="text-subtle" />
          </button>
          {isOpen && (
            <Submenu label={entry.label} anchor={openers.current.get(entry.label) ?? null}>
              <MenuList
                entries={entry.submenu}
                onChoose={onChoose}
                onLeave={closeSub}
                autoFocus={sub?.focus ?? false}
              />
            </Submenu>
          )}
        </div>
      );
    }
    return (
      // biome-ignore lint/a11y/useAriaPropsSupportedByRole: aria-checked is set only on menuitemradio
      <button
        key={entry.label}
        ref={ref}
        type="button"
        role={entry.isSelected === undefined ? "menuitem" : "menuitemradio"}
        aria-checked={entry.isSelected}
        onPointerEnter={() => setSub(null)}
        onClick={() => onChoose(entry)}
        className={ITEM}
      >
        {entry.isSelected !== undefined && (
          <Icon
            glyph="check"
            className={entry.isSelected ? "mt-0.5 text-selected" : "mt-0.5 invisible"}
          />
        )}
        {entry.icon && <Icon glyph={entry.icon} className="mt-0.5 text-icon-subtle" />}
        <span className="flex flex-col items-start">
          <span className={entry.unavailable ? "text-subtle" : undefined}>{entry.label}</span>
          {(entry.unavailable ?? entry.description) && (
            <span className="text-body-sm text-subtlest">
              {entry.unavailable ?? entry.description}
            </span>
          )}
        </span>
      </button>
    );
  };

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: the arrow keys for the menu's items
    <div ref={own} data-menu-list="" className="py-1" onKeyDown={onKeyDown}>
      {entries.map((entry) =>
        "group" in entry ? (
          // biome-ignore lint/a11y/useSemanticElements: a group of menu items, as ARIA spells it
          <div
            key={entry.group}
            role="group"
            aria-label={entry.group}
            className="border-t border-default py-1 first:border-t-0"
          >
            <p aria-hidden="true" className="px-3 py-1 text-body-sm font-semibold text-subtlest">
              {entry.group}
            </p>
            {entry.items.map(renderItem)}
          </div>
        ) : (
          renderItem(entry)
        ),
      )}
    </div>
  );
}

/**
 * A submenu's surface, beside the item that opens it. It stays inside the menu's own element
 * rather than portalling, so a press on it or the focus moving into it is still inside the menu.
 */
function Submenu({
  label,
  anchor,
  children,
}: {
  label: string;
  anchor: HTMLElement | null;
  children: ReactNode;
}) {
  const surface = useRef<HTMLDivElement>(null);
  const [at, setAt] = useState<{ top: number; left: number } | null>(null);
  useLayoutEffect(() => {
    if (!anchor || !surface.current) return;
    const box = anchor.getBoundingClientRect();
    const size = surface.current.getBoundingClientRect();
    const viewport = { width: window.innerWidth, height: window.innerHeight };
    // Raised by the list's padding, so the submenu's first item sits level with its opener.
    const spot = place(
      { top: box.top - 4, left: box.left, width: box.width, height: box.height },
      size,
      viewport,
      "right-start",
      2,
    );
    // On a phone neither side may fit; it then overlaps the menu rather than leaving the screen.
    const left = Math.min(Math.max(spot.left, 8), viewport.width - 8 - size.width);
    setAt({ top: spot.top, left });
  }, [anchor]);
  return (
    <div
      ref={surface}
      role="menu"
      aria-label={label}
      style={at ?? { top: -9999, left: -9999 }}
      className="fixed z-popup max-w-sm min-w-48 rounded-lg bg-surface-overlay text-body text-default shadow-overlay"
    >
      {children}
    </div>
  );
}
