/** Holds `ui/design-system/catalogue.html` (docs/design-system.md, Reference pages). */

await vi.hoisted(() => import("./referenceEnvironment"));

import { describe, expect, it, vi } from "vitest";
import CataloguePage from "./CataloguePage";
import { referenceHtml } from "./referenceHtml";

describe("the catalogue reference page", () => {
  it("matches ui/design-system/catalogue.html", async () => {
    const html = await referenceHtml("catalogue", <CataloguePage />);
    await expect(html).toMatchFileSnapshot("../../design-system/catalogue.html");
  });

  it("links to the guide and the sample files beside it", async () => {
    const html = await referenceHtml("catalogue", <CataloguePage />);
    expect(html).toContain('href="guide.html"');
    expect(html).toContain('href="sample.html"');
  });

  it("pins the clock, zone and locale, so the file is the same on every machine", async () => {
    const html = await referenceHtml("catalogue", <CataloguePage />);
    expect(html).toContain("11:58 AM");
    expect(html).toMatch(/<time dateTime="2026-09-14T09:12:00Z"[^>]*>3 hours ago<\/time>/);
  });
});
