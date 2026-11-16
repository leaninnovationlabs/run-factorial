// @vitest-environment happy-dom
/** The hover card: a mouse resting on the icon opens it, a touch screen's press opens it and a
 *  second press closes it, keyboard focus opens it, and Escape closes it. */

import { act } from "react";
import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { click, type Mounted, mount, press, settle } from "../../testDom";
import { HoverCard } from "./HoverCard";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.useRealTimers();
});

async function show() {
  view = await mount(
    <HoverCard label="Run metrics" icon="chart">
      <p>Duration 14m</p>
    </HoverCard>,
  );
  return view.container.querySelector("button") as HTMLButtonElement;
}

const card = () => document.querySelector<HTMLElement>('[role="tooltip"]');

async function pointer(type: "pointerover" | "pointerout", target: Element, pointerType: string) {
  await act(async () => {
    target.dispatchEvent(
      new PointerEvent(type, { bubbles: true, pointerType, relatedTarget: document.body }),
    );
  });
}

describe("HoverCard", () => {
  it("draws the icon alone on a server render", () => {
    const html = renderToString(
      <HoverCard label="Run metrics" icon="chart">
        <p>Duration 14m</p>
      </HoverCard>,
    );
    expect(html).toContain('aria-label="Run metrics"');
    expect(html).not.toContain("Duration");
  });

  it("opens while a mouse rests on the icon, and closes once it leaves", async () => {
    const icon = await show();
    await pointer("pointerover", icon, "mouse");
    expect(card()).toBeNull();
    await settle(350);
    expect(card()?.textContent).toBe("Duration 14m");
    expect(icon.getAttribute("aria-describedby")).toBe(card()?.id);
    await pointer("pointerout", icon, "mouse");
    await settle(200);
    expect(card()).toBeNull();
  });

  it("opens on a touch screen's press, not on the hover it fakes, and closes on a second", async () => {
    const icon = await show();
    await pointer("pointerover", icon, "touch");
    await settle(350);
    expect(card()).toBeNull();
    await click(icon);
    expect(card()?.textContent).toBe("Duration 14m");
    await click(icon);
    expect(card()).toBeNull();
  });

  it("closes on Escape", async () => {
    const icon = await show();
    await click(icon);
    expect(card()).not.toBeNull();
    await press("Escape");
    expect(card()).toBeNull();
  });
});
