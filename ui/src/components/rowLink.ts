/**
 * Makes a whole list row navigate to where its own primary link already points.
 *
 * The row's `<Link>` stays exactly where it is. This is a mouse convenience layered over a real
 * link, not a replacement for one: the anchor is what keyboard and screen-reader users tab to and
 * what "open in new tab" acts on, and turning the row itself into a second focusable control
 * would announce every row twice for no gain.
 */

import type { MouseEvent } from "react";
import { useNavigate } from "react-router-dom";

/**
 * Whether a click on the row should navigate. Split out from the hook because these rules are
 * the whole of the behaviour, and each one exists for a click that must NOT be swallowed.
 */
export function shouldFollowRow(click: {
  /** Another handler already claimed it. */
  defaultPrevented: boolean;
  /** Cmd/Ctrl/Shift/Alt — "open elsewhere", which belongs to the browser. */
  modified: boolean;
  /** Landed on a nested link or control, such as the row's GitHub issue link. */
  onInteractiveElement: boolean;
  /** Finished a drag-select, so the reader was copying text rather than choosing a row. */
  hasTextSelection: boolean;
  /** Landed outside the row's own DOM: in a drawer it opened, portalled to the body. React
   *  still bubbles the click to the row, so the row has to ask where it happened. */
  fromElsewhere: boolean;
}): boolean {
  return (
    !click.defaultPrevented &&
    !click.modified &&
    !click.onInteractiveElement &&
    !click.hasTextSelection &&
    !click.fromElsewhere
  );
}

export function useRowLink(to: string) {
  const navigate = useNavigate();

  return {
    className: "cursor-pointer",
    onClick(event: MouseEvent<HTMLElement>) {
      const follow = shouldFollowRow({
        defaultPrevented: event.defaultPrevented,
        modified: event.metaKey || event.ctrlKey || event.shiftKey || event.altKey,
        onInteractiveElement:
          event.target instanceof Element &&
          event.target.closest("a, button, input, select") !== null,
        hasTextSelection: (window.getSelection()?.toString() ?? "") !== "",
        fromElsewhere:
          !(event.target instanceof Node) || !event.currentTarget.contains(event.target),
      });
      if (follow) navigate(to);
    },
  };
}
