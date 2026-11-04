import { createContext, type ReactNode, useCallback, useContext, useEffect, useState } from "react";
import { safeGet, safeSet } from "../storage";
import { DEFAULT_THEME, resolveThemeChoice, THEME_STORAGE_KEY, type ThemeChoice } from "./themes";

type ThemeContextValue = {
  choice: ThemeChoice;
  setChoice: (choice: ThemeChoice) => void;
};

const ThemeContext = createContext<ThemeContextValue>({
  choice: DEFAULT_THEME,
  setChoice: () => {},
});

// Applied at module load, before React renders: an effect would paint the other palette first and
// swap on the second frame.
const stored = readStoredChoice();
applyTheme(stored);

/**
 * Applies the chosen theme to the document element and remembers it.
 *
 * On `<html>` rather than the app's root, because dialogs and panels render through portals into
 * `document.body`, outside `#root`, and a theme scoped any lower would leave them unthemed.
 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [choice, setChoiceState] = useState<ThemeChoice>(stored);

  useEffect(() => {
    applyTheme(choice);
  }, [choice]);

  const setChoice = useCallback((next: ThemeChoice) => {
    setChoiceState(next);
    safeSet(THEME_STORAGE_KEY, next);
  }, []);

  return <ThemeContext.Provider value={{ choice, setChoice }}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  return useContext(ThemeContext);
}

/** "Match system" is the absence of the attribute, which `tokens.css` answers by media query. */
function applyTheme(choice: ThemeChoice): void {
  // Guarded because this also runs at module load, where the page tests' server renderer has no
  // `document`; an unguarded write would make every module that imports the theme throw.
  if (typeof document === "undefined") return;
  if (choice === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = choice;
}

function readStoredChoice(): ThemeChoice {
  return resolveThemeChoice(safeGet(THEME_STORAGE_KEY));
}
