/** Holds `ui/design-system/guide.html` (docs/design-system.md, Reference pages). */

await vi.hoisted(() => import("./referenceEnvironment"));

import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import GuidePage, { slug } from "./GuidePage";
import { referenceHtml } from "./referenceHtml";

const DOC = readFileSync(new URL("../../../docs/design-system.md", import.meta.url), "utf8");

describe("the guide reference page", () => {
  it("matches ui/design-system/guide.html", async () => {
    const html = await referenceHtml("guide", <GuidePage />);
    await expect(html).toMatchFileSnapshot("../../design-system/guide.html");
  });

  it("renders every section of docs/design-system.md, each reachable from the contents", async () => {
    const html = await referenceHtml("guide", <GuidePage />);
    const sections = [...DOC.matchAll(/^## (.+)$/gm)].map(([, title = ""]) => title);
    expect(sections.length).toBeGreaterThan(5);
    for (const title of sections) {
      expect(html).toContain(`<h2 id="${slug(title)}"`);
      expect(html).toContain(`href="#${slug(title)}"`);
    }
  });

  it("links to the catalogue and the sample files beside it", async () => {
    const html = await referenceHtml("guide", <GuidePage />);
    expect(html).toContain('href="catalogue.html"');
    expect(html).toContain('href="sample.html"');
  });
});
