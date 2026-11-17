/**
 * The assistant docked: the conversation as a column at the page's right edge, which the page
 * narrows for instead of lying under. It is part of the shell's row (`shell/AppShell.tsx`), so it
 * is not an overlay: a drawer or a dialog covers it and makes it inert with the rest of the page.
 *
 * Its width is the reader's, between `DOCK_MIN` and `DOCK_MAX`: its left edge is a separator that
 * drags, and that the arrow keys move a step at a time.
 */

import {
  type KeyboardEvent,
  type ReactNode,
  type PointerEvent as ReactPointerEvent,
  useState,
} from "react";
import logoUrl from "../../../logo.svg";
import { clampDock, DOCK_MAX, DOCK_MIN } from "./frames";

const STEP = 16;

export function DockPanel({
  width,
  onResize,
  actions,
  footer,
  children,
}: {
  width: number;
  onResize: (width: number) => void;
  actions: ReactNode;
  footer: ReactNode;
  children: ReactNode;
}) {
  const [dragged, setDragged] = useState<number | null>(null);
  const shown = clampDock(dragged ?? width);

  const drag = (event: ReactPointerEvent) => {
    event.preventDefault();
    const from = { x: event.clientX, width: shown };
    const at = (pointer: PointerEvent) => clampDock(from.width + from.x - pointer.clientX);
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

  // The edge is the panel's left: moving it left makes the panel wider.
  const step = (event: KeyboardEvent) => {
    const by = event.key === "ArrowLeft" ? STEP : event.key === "ArrowRight" ? -STEP : 0;
    if (by === 0) return;
    event.preventDefault();
    onResize(clampDock(shown + by));
  };

  return (
    <aside
      data-assistant-dock
      aria-label="Factorial Assistant"
      style={{ width: shown }}
      className="relative flex shrink-0 flex-col border-l border-default bg-surface"
    >
      {/* biome-ignore lint/a11y/useSemanticElements: a window splitter, which an <hr> cannot be: it is focusable and has a value */}
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label="The assistant's width"
        aria-valuenow={shown}
        aria-valuemin={DOCK_MIN}
        aria-valuemax={DOCK_MAX}
        tabIndex={0}
        onPointerDown={drag}
        onKeyDown={step}
        className="absolute inset-y-0 -left-0.5 z-card w-1 cursor-ew-resize touch-none hover:bg-neutral-hovered"
      />
      <header className="flex h-12 shrink-0 items-center gap-1 border-b border-default pr-2 pl-4">
        <img src={logoUrl} alt="" className="size-5 shrink-0 rounded-xs" />
        <h2 className="min-w-0 flex-1 truncate pl-1 text-heading-xs text-default">Assistant</h2>
        {actions}
      </header>
      <div className="min-h-0 flex-1 overflow-hidden">{children}</div>
      <footer className="border-t border-default px-3 py-3">{footer}</footer>
    </aside>
  );
}
