/**
 * The design foundations, held mechanically (decision 50).
 *
 * Two kinds of check. The compile test runs Tailwind over the real `index.css` and fails if a
 * role class stops producing CSS — the property-scoped namespaces are not in Tailwind's docs, and
 * Tailwind emits nothing at all for a class it cannot resolve, so without this an upgrade could
 * quietly drop every colour on the page. The ratchet scans the source for the things the design
 * system replaces and fails when any count rises; when one falls, the baseline here is lowered in
 * the same change, so it cannot creep back.
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { compile } from "tailwindcss";
import ts from "typescript";
import { describe, expect, it } from "vitest";
import { PULSE_MS } from "../components/ui/motion";

const SRC = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);

async function tailwind() {
  const entry = join(SRC, "index.css");
  return compile(readFileSync(entry, "utf8"), {
    base: SRC,
    from: entry,
    loadStylesheet: async (id, base) => {
      const path =
        id === "tailwindcss" ? require.resolve("tailwindcss/index.css") : resolve(base, id);
      return { path, base: dirname(path), content: readFileSync(path, "utf8") };
    },
  });
}

/** The declarations Tailwind writes for one class, or "" when it writes nothing. A class can open
 *  its own rule (`.text-subtle {`) or sit inside a wrapper (`:where(.divide-default > …) {`). */
function rule(css: string, candidate: string): string {
  const selector = `.${candidate.replace(/[:/.[\]]/g, (c) => `\\${c}`)}`;
  const match = new RegExp(`${selector.replace(/[\\.]/g, (c) => `\\${c}`)}(?![\\w-])`).exec(css);
  if (!match) return "";
  const open = css.indexOf("{", match.index);
  return css.slice(match.index, css.indexOf("}", open) + 1);
}

describe("the role classes", () => {
  const EXPECTED: Record<string, string> = {
    "text-subtle": "color: var(--ds-text-subtle)",
    "text-default": "color: var(--ds-text)",
    "text-icon-danger": "color: var(--ds-icon-danger)",
    "bg-surface-sunken": "background-color: var(--ds-surface-sunken)",
    "bg-table-head": "background-color: var(--table-head)",
    "bg-neutral-subtle-hovered": "background-color: var(--ds-background-neutral-subtle-hovered)",
    "border-input": "border-color: var(--ds-border-input)",
    "divide-default": "border-color: var(--ds-border)",
    "outline-focused": "outline-color: var(--ds-border-focused)",
    "ring-focused": "--tw-ring-color: var(--ds-border-focused)",
    "z-modal": "z-index: 510",
    "rounded-sm": "border-radius: var(--ds-radius-small)",
    "shadow-overlay": "var(--ds-shadow-overlay)",
    "ease-out-bold": "var(--ds-easing-out-bold)",
    "text-heading-lg": "font-size: 1.5rem",
    "text-body-sm": "font-size: 0.75rem",
    "opacity-disabled": "opacity: var(--ds-opacity-disabled)",
    "max-w-page-narrow": "max-width: 36rem",
  };

  it("resolves every namespace to its token", async () => {
    const compiler = await tailwind();
    const css = compiler.build(Object.keys(EXPECTED));
    for (const [candidate, declaration] of Object.entries(EXPECTED)) {
      expect(rule(css, candidate), candidate).toContain(declaration);
    }
  });

  it("gives a heading its weight and line height with its size", async () => {
    const css = (await tailwind()).build(["text-heading-lg"]);
    const heading = rule(css, "text-heading-lg");
    expect(heading).toContain("line-height: var(--tw-leading, 1.75rem)");
    expect(heading).toContain("font-weight: var(--tw-font-weight, var(--ds-font-weight-bold))");
  });

  it("keeps sizes and colours apart, so neither name shadows the other", async () => {
    const css = (await tailwind()).build(["text-body", "text-subtlest"]);
    expect(rule(css, "text-body")).toContain("font-size");
    expect(rule(css, "text-body")).not.toContain("color:");
    expect(rule(css, "text-subtlest")).toContain("color:");
    expect(rule(css, "text-subtlest")).not.toContain("font-size");
  });

  it("compiles nothing for Tailwind's own palette, sizes and shadows, which the roles replace", async () => {
    // Switched off in `index.css`, so a class naming one fails quietly in review instead of
    // shipping a colour no theme can retint.
    const stock = ["bg-slate-100", "text-slate-500", "bg-white", "text-sm", "text-xs", "shadow-md"];
    const css = (await tailwind()).build(stock);
    for (const candidate of stock) expect(rule(css, candidate), candidate).toBe("");
  });

  it("draws a pressed or inactive state over a hover, whatever order the classes are written", async () => {
    // A design primitive's appearance is one complete class set, and it relies on Tailwind's own
    // variant order rather than on where a class sits in the string: an inactive button's grey must win
    // over the hover it would otherwise take.
    const css = (await tailwind()).build([
      "aria-disabled:bg-disabled",
      "hover:bg-neutral-hovered",
      "active:bg-neutral-pressed",
    ]);
    const hover = css.indexOf(".hover\\:bg-neutral-hovered");
    expect(hover).toBeGreaterThan(-1);
    expect(css.indexOf(".active\\:bg-neutral-pressed")).toBeGreaterThan(hover);
    expect(css.indexOf(".aria-disabled\\:bg-disabled")).toBeGreaterThan(hover);
  });
});

describe("the layers", () => {
  it("stacks a popup above the drawers and dialogs it opens from, and below the flags", () => {
    const css = readFileSync(join(SRC, "index.css"), "utf8");
    const z = (name: string) => Number(css.match(new RegExp(`--z-index-${name}: (\\d+);`))?.[1]);
    expect(z("popup")).toBeGreaterThan(z("modal"));
    expect(z("popup")).toBeLessThan(z("flag"));
  });
});

describe("tokens.css", () => {
  const tokens = readFileSync(join(SRC, "theme/tokens.css"), "utf8");

  /** The declarations of the block that opens with `selector`, as `name: value` lines. */
  function declarations(selector: string, after = 0): string[] {
    const start = tokens.indexOf(`${selector} {`, after);
    expect(start, selector).toBeGreaterThan(-1);
    const body = tokens.slice(tokens.indexOf("{", start) + 1, tokens.indexOf("}", start));
    // By declaration, not by line: the formatter wraps a long value at a different column in the
    // indented media block than at the top level.
    return body
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .split(";")
      .map((declaration) => declaration.replace(/\s+/g, " ").trim())
      .filter(
        (declaration) => declaration.startsWith("--") || declaration.startsWith("color-scheme"),
      );
  }

  it("writes the chosen dark theme and the system's dark theme with the same values", () => {
    // "Match system" is answered in CSS, so the dark values exist twice; a drift between the two
    // copies would make the two ways of asking for dark disagree.
    const chosen = declarations(':root[data-theme="dark"]');
    const system = declarations(
      ':root:not([data-theme="light"])',
      tokens.indexOf("@media (prefers-color-scheme: dark)"),
    );
    expect(system).toEqual(chosen);
    expect(chosen.length).toBeGreaterThan(100);
  });

  it("defines every token the light theme defines in the dark theme too", () => {
    const names = (lines: string[]) => lines.map((line) => line.split(":")[0]);
    const light = names(declarations(":root", tokens.indexOf("/* Light")));
    expect(names(declarations(':root[data-theme="dark"]'))).toEqual(light);
  });

  it("gives the primary its near-black, not ADS brand blue", () => {
    expect(declarations(":root", tokens.indexOf("/* Light"))).toContain(
      "--ds-background-neutral-bold: #292a2e",
    );
  });

  it("holds the pulse the reticles' phase clock divides by", () => {
    // A pip mounting late starts partway through the beat so its rectangle breathes as one; the
    // offset is taken modulo `PULSE_MS`, so a pulse retuned here alone would put pips out of step.
    expect(declarations(":root")).toContain(`--motion-pulse: ${PULSE_MS / 1000}s`);
  });

  type Rgba = [number, number, number, number];

  /** One theme's colours by name, following `var(--ds-…)` to the value it names. */
  function palette(lines: string[]): (name: string) => Rgba {
    const values = new Map(
      lines.map((line) => [line.slice(0, line.indexOf(":")), line.slice(line.indexOf(":") + 1)]),
    );
    const colour = (name: string): Rgba => {
      const value = values.get(name)?.trim();
      if (value === undefined) throw new Error(`the theme has no ${name}`);
      const reference = value.match(/^var\((--ds-[\w-]+)\)$/)?.[1];
      if (reference) return colour(reference);
      const hex = value.slice(1);
      const full = hex.length <= 4 ? [...hex].map((digit) => digit + digit).join("") : hex;
      const [r, g, b] = [0, 2, 4].map((at) => Number.parseInt(full.slice(at, at + 2), 16));
      const alpha = full.length === 8 ? Number.parseInt(full.slice(6), 16) / 255 : 1;
      return [r ?? 0, g ?? 0, b ?? 0, alpha];
    };
    return colour;
  }

  /** WCAG contrast of `ink` on `ground`, each translucent one laid over what is under it. */
  function contrast(theme: (name: string) => Rgba, ink: string, ground: string): number {
    const over = (top: Rgba, under: Rgba): Rgba => {
      const [r, g, b] = [0, 1, 2].map(
        (i) => top[3] * (top[i] ?? 0) + (1 - top[3]) * (under[i] ?? 0),
      );
      return [r ?? 0, g ?? 0, b ?? 0, 1];
    };
    const luminance = ([r, g, b]: Rgba) => {
      const linear = (value: number) => {
        const s = value / 255;
        return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
      };
      return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
    };
    const back = over(theme(ground), theme("--ds-surface"));
    const [high, low] = [luminance(over(theme(ink), back)), luminance(back)].sort((a, b) => b - a);
    return ((high ?? 0) + 0.05) / ((low ?? 0) + 0.05);
  }

  it("reads at AA in both themes: text at 4.5:1, a control's edge at 3:1", () => {
    // ADS designs these pairs to AA. A value edited here by hand is what could break one, and
    // nothing on screen would say so until someone could not read it.
    const text = [
      ["--ds-text", "--ds-surface"],
      ["--ds-text-subtle", "--ds-surface"],
      ["--ds-text-subtlest", "--ds-surface"],
      ["--ds-text-subtlest", "--ds-surface-sunken"],
      ["--ds-text-subtle", "--ds-background-neutral"],
      ["--ds-text-inverse", "--ds-background-neutral-bold"],
      ["--google-button-text", "--google-button-fill"],
      ["--ds-text-selected", "--ds-background-selected"],
      ...["danger", "warning", "success", "information", "discovery"].map((role) => [
        `--ds-text-${role}`,
        "--ds-surface",
      ]),
      ...["danger", "warning", "success"].map((role) => [
        `--ds-text-${role}`,
        `--ds-background-${role}`,
      ]),
    ];
    const edges = [
      ["--ds-border-input", "--ds-surface"],
      ["--ds-border-focused", "--ds-surface"],
      ["--ds-border-selected", "--ds-surface"],
      ["--ds-icon-subtle", "--ds-surface"],
      ["--google-button-border", "--ds-surface"],
    ];
    const themes = {
      light: palette(declarations(":root", tokens.indexOf("/* Light"))),
      dark: palette(declarations(':root[data-theme="dark"]')),
    };
    for (const [name, theme] of Object.entries(themes)) {
      for (const [ink = "", ground = ""] of text) {
        expect(contrast(theme, ink, ground), `${name}: ${ink} on ${ground}`).toBeGreaterThanOrEqual(
          4.5,
        );
      }
      for (const [edge = "", ground = ""] of edges) {
        expect(
          contrast(theme, edge, ground),
          `${name}: ${edge} on ${ground}`,
        ).toBeGreaterThanOrEqual(3);
      }
    }
  });
});

// --- the ratchet ------------------------------------------------------------------------------

function files(dir: string, extensions: string[]): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return files(path, extensions);
    return extensions.some((extension) => name.endsWith(extension)) ? [path] : [];
  });
}

const isTest = (path: string) => /\.test\.tsx?$/.test(path) || /\.fixture\.tsx?$/.test(path);
const SOURCE = files(SRC, [".ts", ".tsx"]).filter((path) => !isTest(path));
const CSS = files(SRC, [".css"]);

function count(
  paths: string[],
  pattern: RegExp,
  skip: (path: string) => boolean = () => false,
  read: (text: string) => string = (text) => text,
) {
  let total = 0;
  for (const path of paths) {
    if (skip(path)) continue;
    total += read(readFileSync(path, "utf8")).match(pattern)?.length ?? 0;
  }
  return total;
}

/** A file's code alone: an issue cited in a comment (`#167`) reads exactly like a short hex. The
 *  `//` rule leaves a URL's `https://` alone. */
const withoutComments = (text: string) =>
  text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");

const PALETTE =
  /\b(?:[a-z-]+:)*(?:text|bg|border|ring|divide|outline|fill|stroke|placeholder|decoration|accent|caret|from|to|via)-(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d{2,3}(?:\/\d+)?\b|\b(?:[a-z-]+:)*(?:text|bg|border|ring|divide)-(?:white|black)(?:\/\d+)?\b/g;
const HEX = /#[0-9a-fA-F]{3,8}\b/g;
const ARBITRARY_SIZE = /\btext-\[\d/g;
const OFF_SCALE_SPACING =
  /\b-?(?:p|px|py|pt|pb|pl|pr|m|mx|my|mt|mb|ml|mr|gap|gap-x|gap-y|space-x|space-y|inset|top|left|right|bottom)-(?:2\.5|3\.5|9)\b/g;
const TINY_FONT = /font-size:\s*(?:0\.[0-6]\d*rem|0\.7[0-4]\d*rem|(?:[0-9]|1[01])(?:\.\d+)?px)/g;
/** Any way back to a link colour: the utility, the mapping that would define it, and the ADS
 *  values behind it. A link here is marked by weight and underline, never by colour. */
const LINK_COLOUR = /\b(?:[a-z-]+:)*text-link\b|--(?:ds-link|text-color-link)\b/g;

/** Hex a stylesheet or component may still hold: brand assets drawn to someone else's spec — the
 *  mark's plate. */
const HEX_ALLOWED = (path: string) =>
  path.endsWith("theme/tokens.css") ||
  path.endsWith("LogoPlate.tsx") ||
  path.endsWith("logoPlate.css");

/** Where the design primitives live: the one place a raw control is the implementation, not a
 *  copy. */
const isDesignPrimitive = (path: string) => path.includes(`${join("components", "ui")}/`);

/** The two drawings whose marks are the controls — a board cell, a lane heading, a graph node —
 *  and cannot be a `Button` without ceasing to be the picture. */
const isVisualisation = (path: string) =>
  /components\/(?:factory-board\/(?:Board|Band)|flow\/FlowGraph)\.tsx$/.test(path);

/** Words that are names rather than shouting, and stay capitalised in sentence case. */
const ACRONYMS = new Set([
  "API",
  "CI",
  "CLI",
  "CSS",
  "GET",
  "GH",
  "HTML",
  "HTTP",
  "HTTPS",
  "ID",
  "JSON",
  "MCP",
  "MR",
  "OK",
  "PATCH",
  "POST",
  "PR",
  "PUT",
  "DELETE",
  "SHA",
  "SSH",
  "SSO",
  "UI",
  "URL",
  "USD",
  "YAML",
  "OFL",
  "OS",
  "ADS",
  "UTC",
  "AM",
  "PM",
  "NFKD",
]);

/** Copy written in capitals: at least one all-caps word that is not an acronym, and no lower
 *  case. A constant's name (`GH_TOKEN`), a path or a colour is not copy, so `_ / . $ #` skip. */
function shouts(text: string): boolean {
  const trimmed = text.trim();
  if (!/[A-Z]{2}/.test(trimmed) || /[a-z]/.test(trimmed) || /[_/.$#]/.test(trimmed)) return false;
  return (trimmed.match(/[A-Z]{2,}/g) ?? []).some((word) => !ACRONYMS.has(word));
}

/** A string a reader could see: not a module path, a type, or the key of an object. */
function isCopy(node: ts.StringLiteral | ts.NoSubstitutionTemplateLiteral): boolean {
  const parent = node.parent;
  if (ts.isImportDeclaration(parent) || ts.isExportDeclaration(parent)) return false;
  if (ts.isLiteralTypeNode(parent) || ts.isElementAccessExpression(parent)) return false;
  return !(ts.isPropertyAssignment(parent) && parent.name === node);
}

type Markup = {
  titleOnElements: number;
  disabledWithTitle: number;
  buttonOverrides: number;
  rawButtons: number;
  capitalCopy: number;
};

/** One pass over every component with the TypeScript parser, for what a regex cannot see:
 *  JSX attributes can hold `=>`, and copy is JSX text or a string, never an identifier. */
function scanMarkup(): Markup {
  const out: Markup = {
    titleOnElements: 0,
    disabledWithTitle: 0,
    buttonOverrides: 0,
    rawButtons: 0,
    capitalCopy: 0,
  };
  for (const path of SOURCE) {
    if (!path.endsWith(".tsx") && !path.includes(`${join("components")}/`)) continue;
    const text = readFileSync(path, "utf8");
    const file = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    const visit = (node: ts.Node): void => {
      if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
        const tag = node.tagName.getText(file);
        const attributes = new Map<string, ts.JsxAttribute>();
        for (const attribute of node.attributes.properties) {
          if (ts.isJsxAttribute(attribute)) attributes.set(attribute.name.getText(file), attribute);
        }
        const intrinsic = /^[a-z]/.test(tag);
        // An iframe's `title` is its accessible name, not a tooltip, and the linter requires it.
        if (intrinsic && tag !== "iframe" && attributes.has("title")) {
          out.titleOnElements += 1;
          if (attributes.has("disabled")) out.disabledWithTitle += 1;
        }
        if (tag === "button" && !isDesignPrimitive(path) && !isVisualisation(path))
          out.rawButtons += 1;
        if (tag === "Button") {
          const className = attributes.get("className")?.initializer;
          if (className && /\b(?:p[xytblr]?|text)-/.test(className.getText(file))) {
            out.buttonOverrides += 1;
          }
        }
      }
      if (ts.isJsxText(node) && shouts(node.getText(file))) out.capitalCopy += 1;
      if (
        (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) &&
        isCopy(node) &&
        shouts(node.text)
      ) {
        out.capitalCopy += 1;
      }
      ts.forEachChild(node, visit);
    };
    visit(file);
  }
  return out;
}

/** What each count is now. A change that lowers one lowers it here too, in the same commit. */
const BASELINE = {
  /** `text-slate-500`, `bg-red-50`: a palette step where a role belongs. */
  paletteClasses: 0,
  /** A colour written into a stylesheet rather than read from a token. */
  hexInCss: 0,
  hexInTsx: 0,
  /** `text-[13px]`: a size off the type scale. */
  arbitraryTextSizes: 0,
  /** `py-2.5`, `px-3.5`, `gap-9`: steps the ADS spacing scale does not have. */
  offScaleSpacing: 0,
  /** A stylesheet font size under 12px, the floor for readable text. */
  tinyFontSizes: 0,
  /** `text-link`, or a token that would give it a value. Links take the colour of the text
   *  around them; weight and an underline are what say they are links. */
  linkColour: 0,
  /** A native tooltip, which shows on mouse hover only: never on focus, touch, or a disabled
   *  control. What it says belongs on the page or in a `Tooltip` on a focusable trigger. */
  titleOnElements: 0,
  disabledWithTitle: 0,
  /** Padding or size classes on a `<Button>`, which lose to its own on Tailwind's class order. */
  buttonOverrides: 0,
  /** A `<button>` built by hand outside the design primitives. */
  rawButtons: 0,
  /** Copy in capitals (`METRICS`, `DESC`); ADS writes every label in sentence case. */
  capitalCopy: 0,
};

describe("the ratchet", () => {
  const now = {
    paletteClasses: count(SOURCE, PALETTE),
    hexInCss: count(CSS, HEX, HEX_ALLOWED),
    hexInTsx: count(SOURCE, HEX, HEX_ALLOWED, withoutComments),
    arbitraryTextSizes: count(SOURCE, ARBITRARY_SIZE),
    offScaleSpacing: count(SOURCE, OFF_SCALE_SPACING),
    tinyFontSizes: count(CSS, TINY_FONT),
    linkColour: count(SOURCE, LINK_COLOUR) + count(CSS, LINK_COLOUR),
    ...scanMarkup(),
  };

  for (const [name, baseline] of Object.entries(BASELINE)) {
    it(`holds ${name} at its baseline`, () => {
      const current = now[name as keyof typeof now];
      expect(current, `${name} rose from ${baseline} to ${current}`).toBeLessThanOrEqual(baseline);
      expect(current, `${name} fell to ${current}: lower its baseline here`).toBe(baseline);
    });
  }

  it("reports the files it scanned", () => {
    expect(SOURCE.map((path) => relative(SRC, path))).toContain("components/ui/Button.tsx");
  });
});
