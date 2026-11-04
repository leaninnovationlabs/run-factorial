/**
 * The window's width classes, as the shell and the pages read them in script. CSS reads the same
 * lines through Tailwind's `md` and `lg` (48rem and 64rem, ADS's `s` and `m`); a component reaches
 * for these only where the layout changes WHAT it draws — a list for a board, cards for a table —
 * rather than how it is laid out, which a class says better.
 *
 * Phone is the narrow class: below `md`, where the dashboard is for its two primary tasks,
 * tickets and the assistant (docs/design-system.md, "Phone width").
 */

import { useEffect, useState } from "react";

export const PHONE = "(max-width: 767.98px)";
export const BELOW_LG = "(max-width: 1023.98px)";

/** Whether `query` matches now. False where there is no `window`: node and the server render. */
export function matchesMedia(query: string): boolean {
  return typeof window !== "undefined" && window.matchMedia?.(query).matches === true;
}

export function useMedia(query: string): boolean {
  const [matches, setMatches] = useState(() => matchesMedia(query));
  useEffect(() => {
    const list = window.matchMedia?.(query);
    if (!list) return;
    const change = () => setMatches(list.matches);
    change();
    list.addEventListener("change", change);
    return () => list.removeEventListener("change", change);
  }, [query]);
  return matches;
}

/** Below `md`: the phone layout. */
export function useIsPhone(): boolean {
  return useMedia(PHONE);
}
