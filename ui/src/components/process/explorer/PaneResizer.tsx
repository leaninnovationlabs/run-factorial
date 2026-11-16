import { type KeyboardEvent, useState } from "react";
import { cx } from "../../ui";

/** One press of an arrow key. */
const STEP = 16;

/**
 * The file panel's right border, as the thing that moves it: drag it, or focus it and use the
 * arrow keys (Home and End go to either limit). It reports the width asked for and leaves the
 * limits to the caller, which knows how wide the editor is.
 *
 * A window splitter, as ARIA names it: a focusable `separator` that carries the panel's width.
 */
export function PaneResizer({
  width,
  min,
  max,
  left,
  onResize,
  onDone,
}: {
  width: number;
  min: number;
  max: number;
  /** The panel's left edge in the window, read as the pointer moves. */
  left: () => number;
  onResize: (width: number) => void;
  /** The drag ended, or a key moved it: the width is the one to keep. */
  onDone: () => void;
}) {
  const [dragging, setDragging] = useState(false);

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const next =
      event.key === "ArrowRight"
        ? width + STEP
        : event.key === "ArrowLeft"
          ? width - STEP
          : event.key === "Home"
            ? min
            : event.key === "End"
              ? max
              : null;
    if (next === null) return;
    event.preventDefault();
    onResize(next);
    onDone();
  }

  return (
    // biome-ignore lint/a11y/useSemanticElements: a window splitter is a focusable separator with a value, which <hr> cannot be
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize the file panel"
      aria-valuenow={Math.round(width)}
      aria-valuemin={min}
      aria-valuemax={max}
      tabIndex={0}
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        event.currentTarget.setPointerCapture?.(event.pointerId);
        setDragging(true);
      }}
      onPointerMove={(event) => {
        if (dragging) onResize(event.clientX - left());
      }}
      onPointerUp={(event) => {
        if (!dragging) return;
        event.currentTarget.releasePointerCapture?.(event.pointerId);
        setDragging(false);
        onDone();
      }}
      onPointerCancel={() => setDragging(false)}
      onKeyDown={onKeyDown}
      className={cx(
        // The panel's border itself, with a few pixels over the editor's edge to catch it by.
        "relative z-10 -mr-1.5 hidden w-1.5 shrink-0 cursor-col-resize touch-none border-l transition-colors md:block",
        dragging ? "border-focused" : "border-default hover:border-bold",
      )}
    />
  );
}
