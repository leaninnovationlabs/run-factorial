/**
 * The theme choices: light, dark, and matching the system.
 *
 * A choice is an attribute on the document element (see `ThemeProvider`): `data-theme="light"`,
 * `data-theme="dark"`, or no attribute at all for "match system", which `theme/tokens.css`
 * answers with `prefers-color-scheme`. That is also why matching the system is the default: with
 * no attribute the first paint is already right, before any script has run.
 */

export const THEMES = [
  { id: "light", label: "Light" },
  { id: "dark", label: "Dark" },
  { id: "system", label: "Match system theme" },
] as const;

export type ThemeChoice = (typeof THEMES)[number]["id"];

export const DEFAULT_THEME: ThemeChoice = "system";

/** Where the choice is kept. Read by `ThemeProvider` only. */
export const THEME_STORAGE_KEY = "sf-ui:theme";

/**
 * Narrow an untrusted value — a stored one, a hand-edited one — to a choice that exists. A
 * browser still holding a retired id lands on the default rather than on no theme at all.
 */
export function resolveThemeChoice(raw: string | null | undefined): ThemeChoice {
  return THEMES.some((theme) => theme.id === raw) ? (raw as ThemeChoice) : DEFAULT_THEME;
}
