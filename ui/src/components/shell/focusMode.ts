import { createContext, useContext, useEffect } from "react";

/**
 * A page that holds the whole of `<main>` for a while — the config editor — asks the shell to put
 * the side navigation away until it lets go. The page keeps its own way back; leaving the page
 * lets go too, so the navigation never stays hidden behind it.
 */
export const FocusModeContext = createContext<(on: boolean) => void>(() => {});

export function useFocusMode(on: boolean): void {
  const set = useContext(FocusModeContext);
  useEffect(() => {
    set(on);
    return () => set(false);
  }, [on, set]);
}
