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

describe("HoverCard with words beside its icon", () => {
  it("draws them in the trigger, and a press on them opens the card outside the trigger's box", async () => {
    view = await mount(
      <div style={{ overflow: "hidden" }}>
        <HoverCard label="Pointed at Ticket 212" icon="chart" text="Ticket 212">
          <p>Blocked at build</p>
        </HoverCard>
      </div>,
    );
    const trigger = view.container.querySelector("button") as HTMLButtonElement;
    expect(trigger.textContent).toBe("Ticket 212");
    expect(trigger.getAttribute("aria-label")).toBe("Pointed at Ticket 212");
    await click(trigger);
    expect(card()?.textContent).toBe("Blocked at build");
    // On the page, not in the box that holds the trigger: nothing around the trigger clips it.
    expect(card()?.parentElement).toBe(document.body);
  });
});

describe("HoverCard that holds links", () => {
  const shown = () => document.querySelector<HTMLElement>("[data-hover-card]");

  async function linked() {
    view = await mount(
      <HoverCard label="Pointed at Ticket 212" icon="chart" text="Ticket 212" interactive>
        <a href="/tickets/212">Open the linked page</a>
      </HoverCard>,
    );
    return view.container.querySelector("button") as HTMLButtonElement;
  }

  it("a press moves the focus into the card, so a keyboard reaches its links, and Escape gives it back", async () => {
    const trigger = await linked();
    trigger.focus();
    await click(trigger);
    await settle();
    expect(shown()?.getAttribute("role")).toBe("dialog");
    expect(shown()?.contains(document.activeElement)).toBe(true);
    await press("Escape", document);
    expect(shown()).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("a mouse resting on it shows the card and leaves the focus where it was", async () => {
    vi.useFakeTimers();
    const trigger = await linked();
    const elsewhere = document.createElement("input");
    document.body.append(elsewhere);
    elsewhere.focus();
    await pointer("pointerover", trigger, "mouse");
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });
    expect(shown()?.textContent).toBe("Open the linked page");
    expect(shown()?.getAttribute("role")).toBe("tooltip");
    expect(document.activeElement).toBe(elsewhere);
    elsewhere.remove();
  });

  it("following a link in it closes it", async () => {
    const trigger = await linked();
    await click(trigger);
    const link = shown()?.querySelector("a") as HTMLAnchorElement;
    link.addEventListener("click", (event) => event.preventDefault());
    await click(link);
    expect(shown()).toBeNull();
  });
});

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
