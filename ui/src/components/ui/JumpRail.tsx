/**
 * An in-page rail: one button per section of a long region, the section under the reading line
 * marked as the one you are at. It is the orientation and the jump control at once — the ticket's
 * attempts, the configuration's requirements.
 *
 * `aria-current="location"` says which, and so does a bar at the item's edge beside the selected
 * fill: selection is never the fill alone. What an item shows is the caller's; the rail owns the
 * press, the states and the marker.
 */

import type { ReactNode } from "react";
import { cx } from "./cx";

export type JumpRailItem = { key: string; content: ReactNode };

export function JumpRail({
  label,
  items,
  activeKey,
  onPick,
  className,
}: {
  label: string;
  items: JumpRailItem[];
  activeKey: string | null;
  onPick: (key: string) => void;
  /** Layout only: where the rail sits, and whether it scrolls. */
  className?: string;
}) {
  return (
    <nav aria-label={label} className={cx("space-y-0.5", className)}>
      {items.map((item) => {
        const active = item.key === activeKey;
        return (
          <button
            key={item.key}
            type="button"
            onClick={() => onPick(item.key)}
            aria-current={active ? "location" : undefined}
            className={cx(
              "focus-inset relative flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-body transition-colors",
              active
                ? "bg-selected font-medium text-selected"
                : "text-subtle hover:bg-neutral-subtle-hovered hover:text-default",
            )}
          >
            <span
              aria-hidden="true"
              className={cx(
                "absolute inset-y-1 left-0 w-0.5 rounded-full",
                active && "bg-neutral-bold",
              )}
            />
            {item.content}
          </button>
        );
      })}
    </nav>
  );
}
