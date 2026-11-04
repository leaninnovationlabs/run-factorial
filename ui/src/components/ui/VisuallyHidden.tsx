import type { ReactNode } from "react";

/** Text a screen reader reads and the page does not draw: a caption the layout already shows by
 *  position, or a label an icon stands in for. */
export function VisuallyHidden({ children, id }: { children: ReactNode; id?: string }) {
  return (
    <span id={id} className="sr-only">
      {children}
    </span>
  );
}
