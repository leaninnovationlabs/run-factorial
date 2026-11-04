/**
 * The terminal's palette from the ADS tokens, so it follows the theme like the rest of the page.
 *
 * xterm draws on a canvas and cannot read CSS variables, so the values are read off the document
 * and handed over, and read again whenever the theme changes: the switch sets `data-theme` on
 * `<html>`, and "match system" follows the media queries (`ThemeProvider.tsx`).
 */

import type { ITheme } from "@xterm/xterm";

/** ANSI colours from the chart tokens: the one ADS set built to read on both surfaces. */
const PALETTE = {
  background: "--ds-surface-sunken",
  foreground: "--ds-text",
  cursor: "--ds-text",
  cursorAccent: "--ds-surface-sunken",
  selectionBackground: "--ds-background-selected-pressed",
  black: "--ds-chart-gray-bold",
  red: "--ds-chart-red-bold",
  green: "--ds-chart-green-bold",
  yellow: "--ds-chart-yellow-bold",
  blue: "--ds-chart-blue-bold",
  magenta: "--ds-chart-magenta-bold",
  cyan: "--ds-chart-teal-bold",
  white: "--ds-text-subtle",
  brightBlack: "--ds-text-subtlest",
  brightRed: "--ds-chart-red-bold",
  brightGreen: "--ds-chart-green-bold",
  brightYellow: "--ds-chart-yellow-bold",
  brightBlue: "--ds-chart-blue-bold",
  brightMagenta: "--ds-chart-magenta-bold",
  brightCyan: "--ds-chart-teal-bold",
  brightWhite: "--ds-text",
} satisfies Partial<Record<keyof ITheme, string>>;

/** A token left unset is left to xterm's default rather than drawn as an empty colour. */
function readTheme(read: (token: string) => string): ITheme {
  const theme: ITheme = {};
  for (const [key, token] of Object.entries(PALETTE)) {
    const value = read(token).trim();
    if (value) theme[key as keyof typeof PALETTE] = value;
  }
  return theme;
}

export function documentTheme(): ITheme {
  const style = getComputedStyle(document.documentElement);
  return readTheme((token) => style.getPropertyValue(token));
}

/** The page's own mono stack, which leads with the bundled JetBrains Mono. */
export function monoFont(): string {
  const stack = getComputedStyle(document.documentElement).getPropertyValue("--font-mono").trim();
  return stack || '"JetBrains Mono", ui-monospace, Menlo, monospace';
}

/** Calls `onChange` after every theme switch; the returned function stops watching. */
export function watchTheme(onChange: () => void): () => void {
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  // Contrast too: `tokens.css` swaps in its high-contrast values under that query.
  const queries = ["(prefers-color-scheme: dark)", "(prefers-contrast: more)"].map((query) =>
    window.matchMedia(query),
  );
  for (const media of queries) media.addEventListener("change", onChange);
  return () => {
    observer.disconnect();
    for (const media of queries) media.removeEventListener("change", onChange);
  };
}
