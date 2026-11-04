import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { DEFAULT_THEME, resolveThemeChoice, THEMES } from "./themes";

const here = dirname(fileURLToPath(import.meta.url));

describe("resolveThemeChoice", () => {
  it("keeps a value that names a choice", () => {
    for (const theme of THEMES) expect(resolveThemeChoice(theme.id)).toBe(theme.id);
  });

  it("falls back to matching the system for anything else", () => {
    // A retired id left behind in a browser reads as "no preference", and no preference is the
    // system's, which is the one choice whose first paint needs no script.
    expect(DEFAULT_THEME).toBe("system");
    for (const raw of ["wip", "", null]) expect(resolveThemeChoice(raw)).toBe("system");
  });
});

describe("dark", () => {
  it("drops the nav mark's dark half on a dark ground, chosen or the system's", () => {
    // The logo is one file both themes share and its darkest blocks are a shade off the dark
    // ground, so without this the mark renders half-present rather than wrong — the kind of break
    // nobody reports and everybody works around. Both routes to dark need it.
    const css = readFileSync(join(here, "dark.css"), "utf8");
    const chosen = css.match(/\[data-theme="dark"] \[data-nav-mark][^{]*\{[^}]*}/s)?.[0];
    const system = css.match(
      /:root:not\(\[data-theme="light"]\) \[data-nav-mark][^{]*\{[^}]*}/s,
    )?.[0];
    expect(chosen).toContain("mix-blend-mode: lighten");
    expect(system).toContain("mix-blend-mode: lighten");
  });
});
