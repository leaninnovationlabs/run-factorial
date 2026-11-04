/**
 * Every control Show me may point at is on a real element: each name in `ASSIST_ANCHORS` is the
 * `data-assist-anchor` of exactly one element in the dashboard's source. The server's copy of the
 * list is pinned to this one by `test_assist_anchors_pinned.py`.
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, test } from "vitest";
import { ASSIST_ANCHORS, anchorSelector, isAssistAnchor } from "./assistAnchors";

const SRC = resolve(dirname(fileURLToPath(import.meta.url)), "../..");

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.tsx$/.test(name) && !/\.test\.tsx$/.test(name) ? [path] : [];
  });
}

describe("ASSIST_ANCHORS", () => {
  test("each is on exactly one element of the dashboard", () => {
    const placed = new Map<string, string[]>();
    for (const path of sources(SRC)) {
      for (const match of readFileSync(path, "utf8").matchAll(/data-assist-anchor="([^"]+)"/g)) {
        const name = match[1] ?? "";
        placed.set(name, [...(placed.get(name) ?? []), relative(SRC, path)]);
      }
    }
    expect([...placed.keys()].sort()).toEqual([...ASSIST_ANCHORS].sort());
    for (const [name, where] of placed) expect(where, name).toHaveLength(1);
  });

  test("a name off the list is no anchor, and a selector names one exactly", () => {
    expect(isAssistAnchor("ticket.actions")).toBe(true);
    expect(isAssistAnchor("admin.secrets")).toBe(false);
    expect(isAssistAnchor(7)).toBe(false);
    expect(anchorSelector("ticket.actions")).toBe('[data-assist-anchor="ticket.actions"]');
  });
});
