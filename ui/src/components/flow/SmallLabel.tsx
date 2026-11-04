import type { ReactNode } from "react";
import { cx } from "../ui";

/** The small tracked caption used on a grid stage and on graph nodes. */
export function SmallLabel({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cx("flow-small-label", className)}>{children}</span>;
}
