/**
 * Whether a row of controls fits its box, for a view that swaps the row for something narrower
 * when it does not, such as tabs for a dropdown.
 *
 * It measures a hidden copy of the row (`measure`), never the row itself: once the swap is made
 * the row holds the narrower control, so it can no longer say when the original would fit again.
 * The copy must carry the row's own spacing and text classes, at its natural width (`w-max`).
 *
 * The two refs are callbacks, so a row drawn only after its labels were known (a pane that waits
 * for a second answer) is still measured. Both are watched: the box when the panel is resized,
 * the copy when its own width changes, as when a font loads.
 */

import { useLayoutEffect, useState } from "react";

export function useFitsInRow(watch: string): {
  row: (node: HTMLDivElement | null) => void;
  measure: (node: HTMLDivElement | null) => void;
  fits: boolean;
} {
  const [box, setBox] = useState<HTMLDivElement | null>(null);
  const [copy, setCopy] = useState<HTMLDivElement | null>(null);
  const [fits, setFits] = useState(true);

  // biome-ignore lint/correctness/useExhaustiveDependencies: `watch` is what changes the copy's width
  useLayoutEffect(() => {
    if (box === null || copy === null) return;
    const check = () => {
      // A box with no width is hidden or not laid out: it says nothing about the row.
      if (box.clientWidth === 0) return;
      setFits(copy.scrollWidth <= box.clientWidth);
    };
    check();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(check);
    observer.observe(box);
    observer.observe(copy);
    return () => observer.disconnect();
  }, [box, copy, watch]);

  return { row: setBox, measure: setCopy, fits };
}
