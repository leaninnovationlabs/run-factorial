/**
 * The assistant's small window: the conversation floating over the page's bottom right corner,
 * so a person can read it and go on using the page. It is not modal (tech-stack decision 63),
 * and it is no layer of the overlay stack either: it is drawn inside the app, so a drawer or a
 * dialog opened at any time covers it and makes it inert with the rest of the page, and Escape
 * still goes to whatever is open over it.
 *
 * Its size is the reader's: dragged by the top-left corner, the top edge or the left edge, or
 * one of three in `ViewOptions`, which is the keyboard's way to the same thing. It never leaves
 * the browser window, whatever was saved.
 *
 * See-through lets the page show through it: a press of the eye turns it on or off, and a press
 * held longer than `PEEK_MS` is a look that ends when the press does. While it is on the page
 * takes the pointer and the header stays solid, since that is where it is switched off; Escape
 * switches it off too.
 */

import {
  type ReactNode,
  type PointerEvent as ReactPointerEvent,
  useEffect,
  useRef,
  useState,
} from "react";
import logoUrl from "../../../logo.svg";
import { cx, IconButton } from "../ui";
import { pushLayer } from "../ui/layers";
import { clampMini, type Size } from "./frames";

export const PEEK_MS = 350;

type Edge = "corner" | "top" | "left";

const GRIP: Record<Edge, string> = {
  corner:
    "top-0 left-0 size-4 cursor-nwse-resize before:absolute before:top-1 before:left-1 before:size-2 before:rounded-tl-xs before:border-t-2 before:border-l-2 before:border-bold",
  top: "top-0 right-0 left-4 h-1 cursor-ns-resize",
  left: "top-4 bottom-0 left-0 w-1 cursor-ew-resize",
};

function useWindowSize(): Size {
  const [size, setSize] = useState<Size>(() => ({
    width: window.innerWidth,
    height: window.innerHeight,
  }));
  useEffect(() => {
    const measure = () => setSize({ width: window.innerWidth, height: window.innerHeight });
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, []);
  return size;
}

export function MiniWindow({
  size,
  onResize,
  before,
  after,
  footer,
  children,
}: {
  size: Size;
  /** A drag that ended: the size to keep. */
  onResize: (size: Size) => void;
  /** The header's controls, either side of the eye. */
  before?: ReactNode;
  after?: ReactNode;
  footer: ReactNode;
  children: ReactNode;
}) {
  const box = useRef<HTMLElement>(null);
  const within = useWindowSize();
  const [dragged, setDragged] = useState<Size | null>(null);
  const [seeThrough, setSeeThrough] = useState(false);
  const pressedAt = useRef<number | null>(null);
  const wasOn = useRef(false);
  const shown = clampMini(dragged ?? size, within);

  // Escape is the window's only while there is something for it to end.
  useEffect(() => {
    if (seeThrough && box.current) {
      return pushLayer(box.current, () => setSeeThrough(false), { modal: false });
    }
  }, [seeThrough]);

  const drag = (edge: Edge) => (event: ReactPointerEvent) => {
    event.preventDefault();
    const from = { x: event.clientX, y: event.clientY, ...shown };
    const at = (pointer: PointerEvent) =>
      clampMini(
        {
          width: edge === "top" ? from.width : from.width + from.x - pointer.clientX,
          height: edge === "left" ? from.height : from.height + from.y - pointer.clientY,
        },
        within,
      );
    const move = (pointer: PointerEvent) => setDragged(at(pointer));
    const up = (pointer: PointerEvent) => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      setDragged(null);
      onResize(at(pointer));
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  return (
    <aside
      ref={box}
      data-assistant-mini
      data-see-through={seeThrough ? "" : undefined}
      aria-label="Factorial Assistant"
      style={{ width: shown.width, height: shown.height }}
      className={cx(
        "fixed right-4 bottom-[calc(1rem+var(--tab-bar-clearance,0px))] z-layer flex flex-col overflow-hidden rounded-lg",
        seeThrough ? "pointer-events-none" : "bg-surface-overlay shadow-overlay",
      )}
    >
      {!seeThrough &&
        (Object.keys(GRIP) as Edge[]).map((edge) => (
          <div
            key={edge}
            data-grip={edge}
            aria-hidden="true"
            onPointerDown={drag(edge)}
            className={cx("absolute z-card touch-none", GRIP[edge])}
          />
        ))}
      <header
        className={cx(
          "flex h-10 shrink-0 items-center gap-1 pr-2 pl-4",
          seeThrough
            ? "pointer-events-auto rounded-lg bg-surface-overlay shadow-overlay"
            : "border-b border-default",
        )}
      >
        <img src={logoUrl} alt="" className="size-5 shrink-0 rounded-xs" />
        <h2 className="min-w-0 flex-1 truncate pl-1 text-heading-xs text-default">Assistant</h2>
        {before}
        <IconButton
          icon={seeThrough ? "eye-off" : "eye"}
          label="See through"
          spacing="compact"
          isSelected={seeThrough}
          onPointerDown={(event) => {
            // So the release is heard here wherever the pointer has gone, and a peek ends.
            event.currentTarget.setPointerCapture?.(event.pointerId);
            wasOn.current = seeThrough;
            pressedAt.current = event.timeStamp;
            setSeeThrough(true);
          }}
          onPointerUp={(event) => {
            if (pressedAt.current === null) return;
            const held = event.timeStamp - pressedAt.current > PEEK_MS;
            pressedAt.current = null;
            setSeeThrough(held ? false : !wasOn.current);
          }}
          onPointerCancel={() => {
            pressedAt.current = null;
            setSeeThrough(false);
          }}
          // The pointer's presses are handled above; a click with no pointer is the keyboard's.
          onClick={(event) => {
            if (event.detail === 0) setSeeThrough((on) => !on);
          }}
        />
        {after}
      </header>
      <div className={cx("min-h-0 flex-1 overflow-hidden", seeThrough && "opacity-loading")}>
        {children}
      </div>
      <footer
        className={cx("px-3 py-2", seeThrough ? "opacity-loading" : "border-t border-default")}
      >
        {footer}
      </footer>
    </aside>
  );
}
