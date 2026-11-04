/**
 * The shell's page-to-page motion: the page on screen fades out, and the page that replaces it
 * rises into place.
 *
 * The reason this needs a hook at all is that React has no exit: the moment the URL changes, the
 * old page's elements are gone, and there is nothing left to fade. So the shell renders a location
 * it HOLDS rather than the router's own, and this hook decides when to adopt the new one — after
 * the exit has had time to play. `<Routes location>` re-provides the held location to everything
 * under it, so a page's own `useLocation`/`useParams` stay consistent with what is on screen while
 * the swap is in flight.
 *
 * Motion is skipped outright under `prefers-reduced-motion`, here and not only in the stylesheet:
 * a CSS-only opt-out would still make the reader wait out an exit that no longer animates.
 */

import { useEffect, useState } from "react";
import { type Location, useLocation } from "react-router-dom";
import { prefersReducedMotion } from "./components/ui";

/** How long the outgoing page has to fade. Keep in step with `--page-exit-duration`, which is
 *  what actually draws it — this is only the clock that waits for it. */
export const PAGE_EXIT_MS = 130;

export type PagePhase = "in" | "out";

/** What a location change asks of the shell: nothing, an immediate swap, or an exit first. */
export type TransitionStep = "hold" | "swap" | "fade";

export function transitionStep(
  rendered: string,
  incoming: string,
  reducedMotion: boolean,
): TransitionStep {
  if (rendered === incoming) return "hold";
  if (reducedMotion) return "swap";
  // `/` renders a `<Navigate>` and nothing else, so there is no page there to fade — fading it
  // would spend the exit on an empty shell and delay the first real page by that much.
  if (rendered === "/") return "swap";
  return "fade";
}

/** The location the shell should render, and the phase to paint it in. */
export function usePageTransition(): { location: Location; phase: PagePhase } {
  const location = useLocation();
  const [rendered, setRendered] = useState(location);
  const [phase, setPhase] = useState<PagePhase>("in");

  useEffect(() => {
    switch (transitionStep(rendered.pathname, location.pathname, prefersReducedMotion())) {
      case "hold":
        // Same page, different query or hash. Nothing moves, but the held location is what the
        // pages read, so it still has to be adopted.
        if (location !== rendered) setRendered(location);
        return;
      case "swap":
        setRendered(location);
        setPhase("in");
        return;
      default: {
        setPhase("out");
        // Navigating again mid-exit restarts this clock rather than shortening it: the last
        // destination is the one that arrives, which is the only part a reader would notice.
        const timer = window.setTimeout(() => {
          setRendered(location);
          setPhase("in");
        }, PAGE_EXIT_MS);
        return () => window.clearTimeout(timer);
      }
    }
  }, [location, rendered]);

  return { location: rendered, phase };
}
