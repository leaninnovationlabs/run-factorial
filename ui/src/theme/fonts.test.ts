import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, it } from "vitest";

const UI = resolve(dirname(fileURLToPath(import.meta.url)), "../..");

it("loads no stylesheet or font from another origin", () => {
  // Each page view would hand that origin the viewer's address, and an air-gapped install
  // cannot reach it at all.
  const remote =
    /url\(\s*["']?(https?:)?\/\/|@import\s+["'](https?:)?\/\/|<link[^>]+href=["'](https?:)?\/\//;
  const files = [
    "index.html",
    ...readdirSync(join(UI, "src"), { recursive: true, encoding: "utf8" })
      .filter((name) => name.endsWith(".css"))
      .map((name) => join("src", name)),
  ];
  expect(files.filter((file) => remote.test(readFileSync(join(UI, file), "utf8")))).toEqual([]);
});
