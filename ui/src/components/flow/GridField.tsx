import type { ReactNode } from "react";
import { cx } from "../ui";

/**
 * A stage with a square lattice that fades before it reaches the edges.
 * Copied from the visual language of the anime.js docs demos, without the library.
 */
export function GridField({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <section className={cx("flow-grid-field", className)}>
      <div className="flow-grid-field__lattice" aria-hidden="true" />
      <div className="flow-grid-field__content">{children}</div>
    </section>
  );
}
