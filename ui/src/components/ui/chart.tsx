/**
 * What the chart design primitives share: a look, a swatch, and one delegated tooltip.
 *
 * A `look` is the name of a `--dash-*` custom property (`components/dashboard/dashboard.css`),
 * a tint of an ADS chart token — so a mark is painted by `var(--look)` and no design primitive
 * ever names a colour. Every mark and swatch also carries `data-chart-look`, which the stylesheet's
 * hatch keys on. A look the stylesheet does not declare paints nothing, which
 * `components/dashboard/tokens.test.ts` guards.
 */

import { type PointerEvent, useState } from "react";
import { cx } from "./cx";

export type Look = string;

/** The custom property a mark paints from. `bg-[var(--look)]` is the class that reads it. */
export function lookStyle(look: Look): Record<string, string> {
  return { "--look": `var(--dash-${look})` };
}

/** The small square beside a label that says which series the label is about. */
export function Swatch({ look, className }: { look: Look; className?: string }) {
  return (
    <span
      aria-hidden="true"
      data-chart-look={look}
      style={lookStyle(look)}
      className={cx("inline-block size-2 shrink-0 rounded-xs bg-[var(--look)]", className)}
    />
  );
}

/**
 * One bubble for every mark in a figure, read off the event rather than bound per mark — the
 * board's pattern (`factory-board/Band.tsx`). Mouse only, and never the only place a fact lives:
 * every figure writes its values as text, so the bubble repeats what is already on the page.
 */
export function useDelegatedTip() {
  const [tip, setTip] = useState<{ anchor: HTMLElement; text: string } | null>(null);
  return {
    tip,
    handlers: {
      onPointerOver: (event: PointerEvent<HTMLElement>) => {
        const hit = (event.target as HTMLElement).closest<HTMLElement>("[data-tip]");
        const text = hit?.getAttribute("data-tip");
        setTip(hit && text ? { anchor: hit, text } : null);
      },
      onPointerLeave: () => setTip(null),
    },
  };
}
