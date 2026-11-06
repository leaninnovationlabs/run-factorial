import { realpathSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { noticesText, packageDir } from "./licenses";

const ROOT = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

describe("the third-party notices", () => {
  it("carry the licence of each package in the bundle, Tailwind's and each font's", () => {
    // MIT and the OFL both ask that the notice go with every copy, and the image is public.
    const react = realpathSync(require.resolve("react"));
    const text = noticesText(
      [react, "\0vite/modulepreload-polyfill", `${ROOT}/src/main.tsx`],
      ROOT,
    );
    expect(text).toMatch(/^react@\d+\.\d+\.\d+ \(MIT\)$/m);
    expect(text).toMatch(/^tailwindcss@\S+ \(MIT\)$/m);
    expect(text).toContain("Copyright (c) Meta Platforms, Inc. and affiliates.");
    for (const font of ["Inter", "JetBrainsMono", "GoogleSans"]) {
      expect(text).toContain(`${font} font (OFL-1.1)`);
    }
    expect(text).toContain("SIL OPEN FONT LICENSE Version 1.1");
  });

  it("name a scoped package by its scope and name", () => {
    const id =
      "/ui/node_modules/.pnpm/@xterm+xterm@6.0.0/node_modules/@xterm/xterm/lib/xterm.mjs?v=1";
    expect(packageDir(`\0${id}`)).toBe(
      "/ui/node_modules/.pnpm/@xterm+xterm@6.0.0/node_modules/@xterm/xterm",
    );
  });
});
