// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { byText, click, type Mounted, mount } from "../../testDom";
import { JumpRail } from "./JumpRail";

let view: Mounted | null = null;
afterEach(async () => {
  await view?.unmount();
  view = null;
});

const ITEMS = [
  { key: "plan#1", content: "plan" },
  { key: "plan#2", content: "plan #2" },
  { key: "review#1", content: "review" },
];

describe("JumpRail", () => {
  it("is a labelled navigation that marks the one section the reader is at", async () => {
    view = await mount(
      <JumpRail label="Attempts" items={ITEMS} activeKey="plan#2" onPick={() => {}} />,
    );
    const nav = view.container.querySelector("nav");
    expect(nav?.getAttribute("aria-label")).toBe("Attempts");
    const current = [...view.container.querySelectorAll('[aria-current="location"]')];
    expect(current.map((item) => item.textContent)).toEqual(["plan #2"]);
    // In shape as well as colour: the bar is drawn on the current item only.
    expect(current[0]?.querySelector(".bg-neutral-bold")).not.toBeNull();
    expect(view.container.querySelectorAll(".bg-neutral-bold")).toHaveLength(1);
  });

  it("jumps to the section pressed, by its key", async () => {
    const onPick = vi.fn();
    view = await mount(
      <JumpRail label="Attempts" items={ITEMS} activeKey={null} onPick={onPick} />,
    );
    await click(byText(view.container, "button", "review"));
    expect(onPick).toHaveBeenCalledWith("review#1");
    expect(view.container.querySelector("[aria-current]")).toBeNull();
  });
});
