import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import type { Plugin } from "vite";

/** Where the build writes the notices, beside `index.html`; the image ships `dist` as it is. */
export const NOTICES_FILE = "third-party-licenses.txt";

/** Packages whose code reaches the bundle outside the module graph: Tailwind inlines its
 *  stylesheet, and Vite its preload helper. */
const INLINED = ["tailwindcss", "vite"];

type Notice = { title: string; license: string; text: string };

/** The package directory a module id lies in, or null for the app's own code. */
export function packageDir(id: string): string | null {
  const path = id.replace(/^\0/, "").split("?")[0];
  const at = path.lastIndexOf("/node_modules/");
  if (at < 0) return null;
  const parts = path.slice(at + "/node_modules/".length).split("/");
  const name = parts[0].startsWith("@") ? parts.slice(0, 2) : parts.slice(0, 1);
  return path.slice(0, at) + ["/node_modules", ...name].join("/");
}

function packageNotice(dir: string): Notice {
  const manifest = JSON.parse(readFileSync(join(dir, "package.json"), "utf8"));
  const file = readdirSync(dir).find((name) => /^(licen[cs]e|copying)/i.test(name));
  return {
    title: `${manifest.name}@${manifest.version}`,
    license: typeof manifest.license === "string" ? manifest.license : "see the package",
    text: file
      ? readFileSync(join(dir, file), "utf8").trim()
      : "The package holds no licence file.",
  };
}

function fontNotices(fonts: string): Notice[] {
  return readdirSync(fonts)
    .filter((name) => name.endsWith("-OFL.txt"))
    .sort()
    .map((name) => ({
      title: `${name.replace(/-OFL\.txt$/, "")} font`,
      license: "OFL-1.1",
      text: readFileSync(join(fonts, name), "utf8").trim(),
    }));
}

/** The notice file's text: each package whose code is in the bundle, then each font. */
export function noticesText(moduleIds: Iterable<string>, root: string): string {
  const dirs = new Set(INLINED.map((name) => join(root, "node_modules", name)));
  for (const id of moduleIds) {
    const dir = packageDir(id);
    if (dir) dirs.add(dir);
  }
  const byTitle = new Map([...dirs].map(packageNotice).map((n) => [n.title, n]));
  const packages = [...byTitle.values()].sort((a, b) => a.title.localeCompare(b.title));
  const notices = [...packages, ...fontNotices(join(root, "src", "theme", "fonts"))];
  const rule = "-".repeat(79);
  return [
    "The Factorial dashboard includes the third-party software and fonts below. Each stays under",
    "its own licence, whose text follows its name.",
    ...notices.map((n) => `\n${rule}\n${n.title} (${n.license})\n${rule}\n\n${n.text}`),
    "",
  ].join("\n");
}

/** Writes the notices into the build: MIT and the OFL both ask that they go with every copy. */
export function thirdPartyLicenses(root: string): Plugin {
  return {
    name: "factorial-third-party-licenses",
    apply: "build",
    generateBundle() {
      this.emitFile({
        type: "asset",
        fileName: NOTICES_FILE,
        source: noticesText(this.getModuleIds(), root),
      });
    },
  };
}
