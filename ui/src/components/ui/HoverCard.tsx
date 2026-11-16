/**
 * Facts behind an icon, in a small card beside it: figures worth a look but not the room they
 * would take in a row, such as a run's model, duration, tokens and cost. It opens while a mouse
 * rests on the icon or the card, while the icon has keyboard focus, and on a press, which pins
 * it; a press is how a touch screen opens it, and a second press, a press outside, Escape or a
 * scroll closes it.
 *
 * It describes the icon (`role="tooltip"`, `aria-describedby`), so a screen reader hears the
 * facts on the icon's focus, and it never takes the focus itself. So it holds text only: a
 * control in it would be out of a keyboard's reach. A non-modal layer (`layers.ts`), so Escape
 * closes it before whatever it sits over. Portalled only while open, so a server render draws
 * the icon alone.
 */

import {
  type FocusEvent,
  type PointerEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { buttonLook } from "./Button";
import { type Placement, place } from "./floating";
import { type Glyph, Icon } from "./icons";
import { pushLayer } from "./layers";

/** As long as a tooltip waits: a pointer passing over the icon on its way elsewhere opens
 *  nothing. */
const OPEN_DELAY = 300;
/** Long enough to cross the gap from the icon to the card without it closing. */
const CLOSE_DELAY = 150;

export function HoverCard({
  label,
  icon,
  placement = "bottom-end",
  children,
}: {
  /** The icon's name, read out, and the card's. */
  label: string;
  icon: Glyph;
  placement?: Placement;
  children: ReactNode;
}) {
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const card = useRef<HTMLDivElement>(null);
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const [pinned, setPinned] = useState(false);
  const [at, setAt] = useState<{ top: number; left: number } | null>(null);
  const open = hovered || focused || pinned;
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);

  const close = useCallback(() => {
    clearTimeout(timer.current);
    setHovered(false);
    setFocused(false);
    setPinned(false);
  }, []);

  // A mouse only: a touch screen's press is a click, and the hover it fakes before one would
  // open the card under the finger and let the click close it again.
  const hover = (on: boolean) => (event: PointerEvent) => {
    if (event.pointerType !== "mouse") return;
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setHovered(on), on ? OPEN_DELAY : CLOSE_DELAY);
  };

  useEffect(() => {
    const surface = card.current;
    if (!open || !surface) return;
    const pop = pushLayer(surface, close, { modal: false });
    const away = (event: Event) => {
      const target = event.target as Node;
      if (trigger.current?.contains(target) || surface.contains(target)) return;
      close();
    };
    // The card is fixed to the window and the icon moves with what scrolls.
    const scrolled = () => close();
    document.addEventListener("pointerdown", away, true);
    window.addEventListener("scroll", scrolled, true);
    return () => {
      pop();
      document.removeEventListener("pointerdown", away, true);
      window.removeEventListener("scroll", scrolled, true);
    };
  }, [open, close]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: new content is a new size, placed anew
  useLayoutEffect(() => {
    if (!open) {
      setAt(null);
      return;
    }
    if (!trigger.current || !card.current) return;
    setAt(
      place(
        trigger.current.getBoundingClientRect(),
        card.current.getBoundingClientRect(),
        { width: window.innerWidth, height: window.innerHeight },
        placement,
      ),
    );
  }, [open, placement, children]);

  return (
    <>
      <button
        ref={trigger}
        type="button"
        aria-label={label}
        aria-expanded={open}
        aria-describedby={open ? id : undefined}
        onPointerEnter={hover(true)}
        onPointerLeave={hover(false)}
        onFocus={(event: FocusEvent<HTMLButtonElement>) => {
          if (event.currentTarget.matches(":focus-visible")) setFocused(true);
        }}
        onBlur={() => setFocused(false)}
        onClick={() => {
          if (pinned) close();
          else {
            clearTimeout(timer.current);
            setPinned(true);
          }
        }}
        className={buttonLook({ appearance: "ghost", spacing: "compact", square: true })}
      >
        <Icon glyph={icon} />
      </button>
      {open &&
        typeof document !== "undefined" &&
        createPortal(
          <div
            ref={card}
            id={id}
            role="tooltip"
            aria-label={label}
            onPointerEnter={hover(true)}
            onPointerLeave={hover(false)}
            style={at ? { top: at.top, left: at.left } : { top: -9999, left: -9999 }}
            className="fixed z-popup rounded-md border border-default bg-surface-overlay text-body-sm text-default shadow-overlay"
          >
            {children}
          </div>,
          document.body,
        )}
    </>
  );
}
