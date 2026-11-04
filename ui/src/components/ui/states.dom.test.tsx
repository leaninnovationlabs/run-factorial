// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { type Mounted, mount } from "../../testDom";
import { ErrorBoundary } from "./states";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.restoreAllMocks();
});

function Broken(): never {
  throw new TypeError("Cannot read properties of undefined (reading 'map')");
}

describe("ErrorBoundary", () => {
  it("says the page failed, and keeps the exception behind Show details", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    view = await mount(
      <ErrorBoundary>
        <Broken />
      </ErrorBoundary>,
    );
    expect(document.body.textContent).toContain("Couldn't show this page");
    const toggle = document.querySelector("button[aria-expanded]");
    expect(toggle?.textContent).toBe("Show details");
    const details = document.getElementById(toggle?.getAttribute("aria-controls") ?? "");
    expect(details?.hidden).toBe(true);
    expect(details?.textContent).toBe("Cannot read properties of undefined (reading 'map')");
  });
});
