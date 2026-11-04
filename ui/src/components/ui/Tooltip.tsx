/**
 * A tooltip, after ADS: shown on hover AND on keyboard focus, after a short delay, closed by
 * Escape, and never the only place a fact lives — "Don't put critical task information in a
 * tooltip" (ADS tooltip usage). It replaces the native `title`, which a keyboard, a touch screen
 * and a disabled control never show.
 *
 * The child must be something that can take focus: a disabled or static element would hide the
 * tooltip from exactly the people a tooltip on it is for. Nothing is portalled until it opens, so
 * a server render draws the trigger alone.
 */

import {
  cloneElement,
  type FocusEvent,
  type KeyboardEvent,
  type MouseEvent,
  type ReactElement,
  type Ref,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { cx } from "./cx";
import { type Placement, place } from "./floating";

type TriggerProps = {
  ref?: Ref<HTMLElement>;
  "aria-describedby"?: string;
  onMouseEnter?: (event: MouseEvent<HTMLElement>) => void;
  onMouseLeave?: (event: MouseEvent<HTMLElement>) => void;
  onFocus?: (event: FocusEvent<HTMLElement>) => void;
  onBlur?: (event: FocusEvent<HTMLElement>) => void;
  onKeyDown?: (event: KeyboardEvent<HTMLElement>) => void;
};

export function Tooltip({
  content,
  position = "top",
  delay = 300,
  describes = false,
  onlyIfTruncated = false,
  children,
}: {
  content: string;
  position?: Placement;
  delay?: number;
  /** Link the text to the trigger as its description. Off when the text IS the trigger's name
   *  (an icon button's label), which would otherwise be read twice. */
  describes?: boolean;
  /** Open only while some of the trigger's text is cut short: the whole of a truncated label
   *  says nothing over one already shown in full. Read at each hover, so a resize needs no
   *  watching. */
  onlyIfTruncated?: boolean;
  children: ReactElement<TriggerProps>;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const anchor = useRef<HTMLElement | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);

  const show = () => {
    clearTimeout(timer.current);
    if (onlyIfTruncated && anchor.current && !cutShort(anchor.current)) return;
    timer.current = setTimeout(() => setOpen(true), delay);
  };
  const hide = () => {
    clearTimeout(timer.current);
    setOpen(false);
  };

  const own = children.props;
  const trigger = cloneElement(children, {
    ref: (node: HTMLElement | null) => {
      anchor.current = node;
      const inner = own.ref;
      if (typeof inner === "function") inner(node);
      else if (inner) (inner as { current: HTMLElement | null }).current = node;
    },
    "aria-describedby": cx(own["aria-describedby"], describes && open && id) || undefined,
    onMouseEnter: (event: MouseEvent<HTMLElement>) => {
      own.onMouseEnter?.(event);
      show();
    },
    onMouseLeave: (event: MouseEvent<HTMLElement>) => {
      own.onMouseLeave?.(event);
      hide();
    },
    onFocus: (event: FocusEvent<HTMLElement>) => {
      own.onFocus?.(event);
      if (event.currentTarget.matches(":focus-visible")) show();
    },
    onBlur: (event: FocusEvent<HTMLElement>) => {
      own.onBlur?.(event);
      hide();
    },
    onKeyDown: (event: KeyboardEvent<HTMLElement>) => {
      own.onKeyDown?.(event);
      // Spent here, so a drawer the trigger sits in stays open for the next press.
      if (event.key === "Escape" && open) {
        event.preventDefault();
        hide();
      }
    },
  } as TriggerProps);

  return (
    <>
      {trigger}
      {open &&
        typeof document !== "undefined" &&
        createPortal(
          <Bubble id={id} anchor={anchor.current} position={position}>
            {content}
          </Bubble>,
          document.body,
        )}
    </>
  );
}

/** Whether the trigger, or anything in it, holds text wider than its box. The descendants count
 *  because the part that truncates is often not the trigger: a heading's title can give way
 *  while its number stays whole. */
function cutShort(node: HTMLElement): boolean {
  return [node, ...node.querySelectorAll("*")].some((part) => part.scrollWidth > part.clientWidth);
}

/**
 * The bubble alone, placed against an element the caller tracks. For a surface with too many
 * marks to wrap each in a `Tooltip` — the board's squares, which are spans inside one focusable
 * cell — and which shows it from a single delegated hover. What it says must also be readable
 * somewhere a keyboard reaches.
 */
export function AnchoredTooltip({
  anchor,
  content,
  position = "top",
}: {
  anchor: HTMLElement;
  content: string;
  position?: Placement;
}) {
  const id = useId();
  if (typeof document === "undefined") return null;
  return createPortal(
    <Bubble id={id} anchor={anchor} position={position}>
      {content}
    </Bubble>,
    document.body,
  );
}

function Bubble({
  id,
  anchor,
  position,
  children,
}: {
  id: string;
  anchor: HTMLElement | null;
  position: Placement;
  children: string;
}) {
  const bubble = useRef<HTMLDivElement>(null);
  const [at, setAt] = useState<{ top: number; left: number } | null>(null);
  // biome-ignore lint/correctness/useExhaustiveDependencies: new text is a new size, placed anew
  useLayoutEffect(() => {
    if (!anchor || !bubble.current) return;
    setAt(
      place(
        anchor.getBoundingClientRect(),
        bubble.current.getBoundingClientRect(),
        { width: window.innerWidth, height: window.innerHeight },
        position,
      ),
    );
  }, [anchor, position, children]);
  return (
    <div
      ref={bubble}
      id={id}
      role="tooltip"
      style={at ? { top: at.top, left: at.left } : { top: -9999, left: -9999 }}
      className="pointer-events-none fixed z-tooltip max-w-60 rounded-sm bg-neutral-bold px-2 py-1 text-body-sm whitespace-pre-line text-inverse shadow-overlay"
    >
      {children}
    </div>
  );
}
