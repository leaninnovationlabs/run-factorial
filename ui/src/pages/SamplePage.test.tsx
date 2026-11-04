/** Holds `ui/design-system/sample.html` (docs/design-system.md, Reference pages). */

await vi.hoisted(() => import("./referenceEnvironment"));

import { describe, expect, it, vi } from "vitest";
import { referenceHtml } from "./referenceHtml";
import SamplePage from "./SamplePage";

describe("the sample reference page", () => {
  it("matches ui/design-system/sample.html", async () => {
    const html = await referenceHtml("sample", <SamplePage />);
    await expect(html).toMatchFileSnapshot("../../design-system/sample.html");
  });

  it("inlines everything it draws with, so the file opens with no server", async () => {
    const html = await referenceHtml("sample", <SamplePage />);
    expect(html).not.toMatch(/<link\b/);
    expect(html).not.toMatch(/<script\b[^>]*\bsrc=/);
    expect(html).toContain("data:font/woff2;base64,");
  });

  it("links to the catalogue file beside it, where the app links to the route", async () => {
    const html = await referenceHtml("sample", <SamplePage />);
    expect(html).toMatch(/<a[^>]*href="catalogue\.html"[^>]*>(?:<[^>]+>)*Catalogue</);
    expect(html).not.toContain('href="/dev/catalogue"');
  });

  it("has one theme switch, in the page's header", async () => {
    const html = await referenceHtml("sample", <SamplePage />);
    expect(html.match(/data-theme-switch=""/g)).toHaveLength(1);
    expect(html).toMatch(/<header[^>]*>(?:(?!<\/header>)[\s\S])*data-theme-switch=""/);
  });

  it("emits the role classes the page names", async () => {
    const html = await referenceHtml("sample", <SamplePage />);
    expect(html).toMatch(/\.bg-surface-sunken\s*{\s*background-color: var\(--ds-surface-sunken\)/);
    expect(html).toContain("--ds-surface:");
  });
});
