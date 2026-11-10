/** Renders a reference page to one self-contained HTML file (docs/design-system.md, Reference pages). */

import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { ReactElement } from "react";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { compile } from "tailwindcss";
import { segmentLook } from "../components/ui";
import { THEMES } from "../theme/themes";
import { REFERENCE_PAGES, type ReferenceName } from "./ReferenceNav";

const SRC = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const FONTS = join(SRC, "theme", "fonts");
const require = createRequire(import.meta.url);

const titleOf = (name: ReferenceName) => `${name.charAt(0).toUpperCase()}${name.slice(1)}`;

/** `index.css` less the stylesheets of features no reference page draws, so a change to the
 *  board or the process graph does not stale the files. `dashboard.css` stays: it holds the chart
 *  looks `StackedBar`, `BarList` and `Legend` paint. */
function entryStylesheet(): string {
  return readFileSync(join(SRC, "index.css"), "utf8").replace(
    /^@import "\.\/(?!theme\/|components\/dashboard\/)[^"]+\.css";\n/gm,
    "",
  );
}

async function stylesheet(candidates: string[]): Promise<string> {
  const compiler = await compile(entryStylesheet(), {
    base: SRC,
    from: join(SRC, "index.css"),
    loadStylesheet: async (id, base) => {
      const path =
        id === "tailwindcss" ? require.resolve("tailwindcss/index.css") : resolve(base, id);
      return { path, base: dirname(path), content: readFileSync(path, "utf8") };
    },
  });
  // Google Sans is the Google sign-in button's face alone; no reference page has a sign-in.
  const css = compiler
    .build(candidates)
    .replace(/@font-face\s*{[^}]*"Google Sans"[^}]*}\s*/g, "")
    .replace(/url\("\.\/fonts\/([^"]+\.woff2)"\)/g, (_, file: string) => {
      const data = readFileSync(join(FONTS, file)).toString("base64");
      return `url("data:font/woff2;base64,${data}")`;
    });
  const unresolved = css.match(/url\((?!"data:)[^)]*\)/g);
  if (unresolved) throw new Error(`a reference page would load from disk: ${unresolved}`);
  return css;
}

/** Every class the markup names, which is every candidate Tailwind needs to emit. */
function classesIn(html: string): string[] {
  const names = new Set<string>();
  for (const [, value] of html.matchAll(/\bclass="([^"]*)"/g)) {
    const decoded = (value ?? "")
      .replace(/&amp;/g, "&")
      .replace(/&#x27;/g, "'")
      .replace(/&quot;/g, '"')
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">");
    for (const name of decoded.split(/\s+/)) if (name) names.add(name);
  }
  return [...names].sort();
}

/** The theme switch's own classes, so the script below can redraw a choice the way
 *  `SegmentedControl` would. */
const LOOK = { on: segmentLook("default", true), off: segmentLook("default", false) };

/** What `ThemeProvider` does with a choice, without React: the page is static. Any other app
 *  link points at a route that exists only in the app, so a click on one stays here. */
const SCRIPT = `
const themes = ${JSON.stringify(THEMES.map((theme) => theme.id))};
const look = ${JSON.stringify(LOOK)};
const buttons = Array.from(document.querySelectorAll("[data-theme-switch] button"));
buttons.forEach((button, index) => {
  button.addEventListener("click", () => {
    const choice = themes[index];
    if (choice === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = choice;
    buttons.forEach((other, at) => {
      other.className = at === index ? look.on : look.off;
      other.setAttribute("aria-pressed", String(at === index));
    });
  });
});
document.addEventListener("click", (event) => {
  const link = event.target.closest('a[href^="/"]');
  if (link) event.preventDefault();
});
`;

function ReferenceBar({ name }: { name: ReferenceName }) {
  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-default bg-surface-raised px-4 py-2 text-body-sm text-subtle">
      <span>
        Design system {name}, generated from{" "}
        <code className="font-mono">ui/src/pages/{titleOf(name)}Page.tsx</code>. See
        docs/design-system.md.
      </span>
    </div>
  );
}

export async function referenceHtml(name: ReferenceName, page: ReactElement): Promise<string> {
  // A link from one reference page to another opens the file beside this one; the route exists
  // only in the app.
  const body = renderToString(
    <MemoryRouter initialEntries={[`/dev/${name}`]}>
      <ReferenceBar name={name} />
      {page}
    </MemoryRouter>,
  ).replace(
    new RegExp(`href="/dev/(${REFERENCE_PAGES.map((entry) => entry.name).join("|")})"`, "g"),
    (_, target: string) => `href="${target}.html"`,
  );
  const css = await stylesheet(classesIn(body));
  const title = titleOf(name);
  return `<!doctype html>
<!-- Generated from ui/src/pages/${title}Page.tsx. Do not edit: run \`make design-system-update\`. -->
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>${title} · Design system</title>
<style>
${css}
</style>
</head>
<body>
${body}
<script>${SCRIPT}</script>
</body>
</html>
`;
}
