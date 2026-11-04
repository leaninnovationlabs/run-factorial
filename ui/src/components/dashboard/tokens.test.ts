/**
 * The `--dash-*` looks: the status looks tinted 30% toward the surface and the categorical ones
 * 15%, the rework looks and `idle` naming another look, the stylesheet and `DASH_LOOKS` naming
 * the same set, and the hatch over exactly the looks that share a colour with another. A look
 * with no declaration paints nothing, a series tinted like a status loses the colour-vision
 * checks, and a rework segment without its hatch is its step's first attempt; nothing else in
 * the suite can see any of them.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { DASH_LOOKS } from "./looks";

const css = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "dashboard.css"), "utf8");
const tokens = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "../../theme/tokens.css"),
  "utf8",
);
const rules = css.replace(/\/\*[\s\S]*?\*\//g, "");
const declared = [...rules.matchAll(/--dash-([\w-]+):\s*([^;]+);/g)].map(([, name, value]) => ({
  name,
  value,
}));
const tint = (share: number) =>
  new RegExp(
    `^color-mix\\(in srgb, var\\(--ds-chart-[\\w-]+\\) ${share}%, var\\(--ds-surface\\)\\)$`,
  );
/** Status looks: judged by contrast, so they take the stronger tint. */
const STATUS_TINT = tint(70);
/** Series, retry and muted: at 70% series 3 and 4 fail the colour-vision checks. */
const SERIES_TINT = tint(85);
const ALIAS = /^var\(--dash-(?:series-\d|muted)\)$/;

describe("the dashboard looks", () => {
  it("tint a status look 30% and a series 15%, and alias the hatched looks", () => {
    expect(declared.length).toBeGreaterThan(0);
    for (const { name = "", value = "" } of declared) {
      const rule =
        name.startsWith("rework-") || name === "idle"
          ? ALIAS
          : name.startsWith("outcome-")
            ? STATUS_TINT
            : SERIES_TINT;
      expect(value, name).toMatch(rule);
    }
    expect(css.match(/#[0-9a-fA-F]{3,8}\b/g)).toBeNull();
  });

  it("reference only tokens and looks that exist, since a dangling var() paints nothing", () => {
    const adsNames = new Set([...tokens.matchAll(/(--ds-[\w-]+)\s*:/g)].map(([, name]) => name));
    const looks = new Set(DASH_LOOKS.map((look) => `--dash-${look}`));
    for (const { name = "", value = "" } of declared) {
      for (const ref of [...value.matchAll(/var\((--[\w-]+)\)/g)].map((m) => m[1] ?? "")) {
        expect(ref.startsWith("--ds-") ? adsNames : looks, `${name} -> ${ref}`).toContain(ref);
      }
    }
  });

  it("declare exactly the looks the sections can name", () => {
    expect(declared.map((look) => look.name).sort()).toEqual([...DASH_LOOKS].sort());
  });

  it("hatch exactly the looks that share a colour with another, since the hatch is all that tells them apart", () => {
    const selectors = (rules.match(/([^{}]+)\{[^}]*background-image:[^}]*\}/)?.[1] ?? "")
      .split(",")
      .map((selector) => selector.trim());
    const hatched = (look: string) =>
      selectors.some((selector) => {
        const [, operator, value = ""] =
          selector.match(/^\[data-chart-look(\^?=)"([\w-]+)"\]$/) ?? [];
        return operator === "^=" ? look.startsWith(value) : operator === "=" && look === value;
      });
    expect(DASH_LOOKS.filter(hatched)).toEqual([
      "rework-1",
      "rework-2",
      "rework-3",
      "rework-4",
      "idle",
    ]);
  });
});
