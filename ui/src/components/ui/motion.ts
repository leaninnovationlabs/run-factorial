/**
 * Motion timings the scripts need, in step with the ADS duration tokens in `theme/tokens.css`
 * (`--ds-duration-*`). A timer that ends a transition cannot read a custom property, so the two
 * numbers a component waits on are written here once.
 */

/** A panel leaving: `--ds-duration-medium` on `--ds-easing-in-practical`. */
export const EXIT_MS = 200;
/** One out-and-back of a selection's pips: `--motion-pulse`. */
export const PULSE_MS = 920;

/** Honour the OS setting. Read at the moment of use rather than cached: it can change under a
 *  running tab, and nothing here is expensive enough to memoise. */
export function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}
