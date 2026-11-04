// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from "vitest";
import { byText, click, type Mounted, mount } from "../../testDom";
import { Panel } from "./Panel";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
});

describe("Panel note", () => {
  it("opens from the About button named after the panel", async () => {
    view = await mount(<Panel title="T" subtitle="S" note="N" />);
    expect(byText(document.body, "*", "N")).toBeNull();
    await click(document.querySelector('button[aria-label="About T"]'));
    expect(byText(document.body, "*", "N")).not.toBeNull();
  });

  it("draws no header rule when a note is all the panel holds", async () => {
    view = await mount(<Panel title="T" note="N" />);
    expect(document.querySelector("header")?.className).not.toContain("border-b");
  });
});
