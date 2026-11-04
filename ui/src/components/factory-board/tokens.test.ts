/**
 * The `--fb-*` tokens: declared on every root that reads them, and holding the values the rest of
 * the app agrees on. No root is inside another — the instrument strip and the issue list are
 * siblings of the board — so a token declared on one selector resolves to nothing everywhere
 * else, which shows up as an invisible legend swatch or an unpainted lane dot rather than as an
 * error.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { BOARD_LANE_TOKEN } from "../../status";
import { LANES } from "./lanes";
import { ROW_PX, ROW_PX_TOUCH } from "./list";
import { RETICLE_PAD } from "./Reticle";

const here = dirname(fileURLToPath(import.meta.url));

const read = (path: string) => readFileSync(join(here, path), "utf8");

/**
 * The selector list of the rule that declares `--fb-empty`.
 *
 * `--fb-empty` is the anchor because it is the one token BOTH sheets set: the dark block remaps
 * only the colours, so the density and geometry tokens appear in the light sheet alone.
 *
 * Comments come out first, then the selector list is what lies between the token's own `{` and
 * the end of the rule before it. Both sheets are heavily commented, and every one of those
 * comments sits exactly where a regex would look for a selector.
 */
function tokenRoots(sheet: string): string[] {
  const css = sheet.replace(/\/\*[\s\S]*?\*\//g, "");
  const at = css.indexOf("--fb-empty:");
  if (at === -1) throw new Error("no rule declares --fb-empty");
  const open = css.lastIndexOf("{", at);
  return css
    .slice(css.lastIndexOf("}", open) + 1, open)
    .replace(/\[data-theme="dark"\]/g, "")
    .split(",")
    .map((selector) => selector.trim())
    .filter(Boolean)
    .sort();
}

describe("the token roots", () => {
  it("declares the values on every root that reads them", () => {
    expect(tokenRoots(read("factory-board.css"))).toEqual([
      ".factory-board",
      ".factory-board__frame",
      ".factory-board__instruments",
      ".factory-board__list",
    ]);
  });
});

describe("the lane colours", () => {
  it("are the status matrix's board colours, so a lane and its lozenge's legend agree", () => {
    // `status.ts` states which chart colour each condition takes on the board, with its two
    // documented exceptions; the stylesheet is where the board actually paints them.
    const css = read("factory-board.css");
    const token = (name: string) => css.match(new RegExp(`--fb-${name}:\\s*([^;]+);`))?.[1];
    expect(token("admitted")).toBe(BOARD_LANE_TOKEN.admitted);
    expect(token("running")).toBe(BOARD_LANE_TOKEN.running);
    expect(token("gate")).toBe(BOARD_LANE_TOKEN.waiting_gate);
    expect(token("error")).toBe(BOARD_LANE_TOKEN.blocked);
    expect(token("retrying")).toBe(BOARD_LANE_TOKEN.retrying);
    expect(token("done")).toBe("var(--fb-admitted)");
  });

  it("reads every colour from a token, so both themes come from the same rules", () => {
    const css = read("factory-board.css").replace(/\/\*[\s\S]*?\*\//g, "");
    expect(css.match(/#[0-9a-fA-F]{3,8}\b/g)).toBeNull();
    expect(css).not.toContain("currentColor");
    // A ground takes a ground's token: a text colour behind a hover would be an opaque grey block.
    expect(css).not.toMatch(/background-color: var\(--ds-(?:text|icon)/);
  });

  it("gives every look a ticket can take a colour", () => {
    // The slot, the row's swatch and the legend's key all paint `var(--look)`. A look with no
    // entry resolves to nothing, and a square with no colour reads as no ticket at all.
    const css = read("factory-board.css");
    for (const look of [...LANES, "muted", "heat-0", "heat-1", "heat-2", "heat-3"]) {
      expect(ruleBody(css, `[data-look="${look}"]`).trim()).toBe(`--look: var(--fb-${look});`);
    }
  });
});

describe("the row height", () => {
  it("is the same number in the stylesheet and in the paging arithmetic", () => {
    // `list.ts` divides the measured list box by `ROW_PX` to decide how many rows a page holds.
    // If `--fb-row` drifts from it nothing breaks visibly — the list just quietly pages one row
    // short, or one row into a scrollbar it is built not to have.
    const declared = read("factory-board.css").match(/--fb-row:\s*([\d.]+)rem/);
    expect(declared?.[1]).toBeDefined();
    expect(Number(declared?.[1]) * 16).toBe(ROW_PX);
  });

  it("is the same number on a phone, where the list stands alone", () => {
    const css = read("factory-board.css");
    const phone = css.slice(css.indexOf("@media (max-width: 47.9375rem)"));
    const declared = phone.match(/--fb-row:\s*([\d.]+)rem/);
    expect(declared?.[1]).toBeDefined();
    expect(Number(declared?.[1]) * 16).toBe(ROW_PX_TOUCH);
  });
});

describe("the reticle's room", () => {
  it("reserves at least as much paper as a pip can reach across", () => {
    // The board pads its right edge by `--fb-reticle-room` so the pips on the last lane are
    // drawn on the board rather than over the end of it. A pip starts `RETICLE_PAD` outside the
    // group and pulses `--fb-reticle-amplitude` further out, so anything less than the two
    // together brings back the six-pixel scrollbar this replaced.
    const css = read("factory-board.css");
    const px = (token: string) => {
      const found = css.match(new RegExp(`--fb-${token}:\\s*([\\d.]+)px`));
      expect(found?.[1]).toBeDefined();
      return Number(found?.[1]);
    };
    expect(px("reticle-room")).toBeGreaterThanOrEqual(RETICLE_PAD + px("reticle-amplitude"));
  });
});

/** The body of the rule whose selector list is exactly `selector`, comments removed. */
function ruleBody(sheet: string, selector: string): string {
  for (const block of sheet.replace(/\/\*[\s\S]*?\*\//g, "").split("}")) {
    const [head, body] = block.split("{");
    if (head?.trim() === selector) return body ?? "";
  }
  return "";
}

describe("what the board makes inert", () => {
  // An empty square and an empty node group are pictures of absence, not targets. Both are taken
  // out of pointer reach in the stylesheet rather than by a handler that has to remember: a hover
  // cue is a promise of a selection, and neither of these has one to make. Asserted here because
  // nothing else can see it — the suite has no DOM, so a rule that stopped matching would show up
  // only as a square that lights under the cursor and does nothing when clicked.
  const css = read("factory-board.css");

  it("takes a square holding no ticket out of pointer reach", () => {
    expect(ruleBody(css, ".factory-board__slot:not([data-ticket])")).toContain(
      "pointer-events: none",
    );
  });

  it("takes a node group holding no issues out of pointer reach", () => {
    expect(ruleBody(css, ".factory-board__lane[aria-disabled]")).toContain("pointer-events: none");
  });
});
