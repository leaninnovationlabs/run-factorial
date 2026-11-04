// @vitest-environment happy-dom
/**
 * The tooltip that replaced `title`: it has to show on keyboard focus as well as on hover, and
 * Escape has to close it without closing the drawer it may be sitting in.
 */

import { act } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { hover, type Mounted, mount, settle, unhover } from "../../testDom";
import { AnchoredTooltip, Tooltip } from "./Tooltip";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
});

const bubble = () => document.querySelector('[role="tooltip"]');
const trigger = () => document.querySelector("button") as HTMLButtonElement;

async function tipped(describes = false) {
  view = await mount(
    <Tooltip content="Most first. Press for fewest first." delay={40} describes={describes}>
      <button type="button" aria-describedby="own">
        Sort
      </button>
    </Tooltip>,
  );
}

describe("Tooltip", () => {
  it("shows after its delay under the pointer, and goes when the pointer leaves", async () => {
    await tipped();
    await hover(trigger());
    await settle(5);
    // Not yet: a pointer crossing the control on its way somewhere else raises nothing.
    expect(bubble()).toBeNull();
    await settle(60);
    expect(bubble()?.textContent).toBe("Most first. Press for fewest first.");
    await unhover(trigger());
    expect(bubble()).toBeNull();
  });

  it("shows on keyboard focus, which a native title never did", async () => {
    await tipped();
    await act(async () => trigger().focus());
    await settle(60);
    expect(bubble()?.textContent).toBe("Most first. Press for fewest first.");
    await act(async () => trigger().blur());
    expect(bubble()).toBeNull();
  });

  it("closes on Escape and spends the key, so a drawer under it stays open", async () => {
    await tipped();
    await hover(trigger());
    await settle(60);
    const escapeKey = new KeyboardEvent("keydown", {
      key: "Escape",
      bubbles: true,
      cancelable: true,
    });
    await act(async () => {
      trigger().dispatchEvent(escapeKey);
    });
    expect(bubble()).toBeNull();
    expect(escapeKey.defaultPrevented).toBe(true);
  });

  it("leaves Escape alone when there is no tooltip to close", async () => {
    await tipped();
    const escapeKey = new KeyboardEvent("keydown", {
      key: "Escape",
      bubbles: true,
      cancelable: true,
    });
    await act(async () => {
      trigger().dispatchEvent(escapeKey);
    });
    expect(escapeKey.defaultPrevented).toBe(false);
  });

  it("describes its trigger only while open, and keeps the trigger's own description", async () => {
    await tipped(true);
    expect(trigger().getAttribute("aria-describedby")).toBe("own");
    await hover(trigger());
    await settle(60);
    const ids = trigger().getAttribute("aria-describedby")?.split(" ") ?? [];
    expect(ids).toContain("own");
    const tipId = ids.find((id) => id !== "own");
    expect(tipId && document.getElementById(tipId)?.textContent).toBe(
      "Most first. Press for fewest first.",
    );
  });

  it("stays shut over a label shown whole, when it only restates a cut one", async () => {
    view = await mount(
      <Tooltip content="#204 Remit-to address is dropped" delay={40} onlyIfTruncated>
        <h1 tabIndex={-1}>
          #204 <span>Remit-to address is dropped</span>
        </h1>
      </Tooltip>,
    );
    const heading = document.querySelector("h1") as HTMLElement;
    // The part that gives way is inside the trigger, not the trigger itself.
    const title = heading.querySelector("span") as HTMLElement;
    const widths = (scroll: number) => {
      Object.defineProperty(title, "scrollWidth", { configurable: true, value: scroll });
      Object.defineProperty(title, "clientWidth", { configurable: true, value: 200 });
    };

    widths(200);
    await hover(heading);
    await settle(60);
    expect(bubble()).toBeNull();
    await unhover(heading);

    widths(340);
    await hover(heading);
    await settle(60);
    expect(bubble()?.textContent).toBe("#204 Remit-to address is dropped");
  });

  it("does not describe a trigger whose name it already is", async () => {
    await tipped(false);
    await hover(trigger());
    await settle(60);
    expect(bubble()).not.toBeNull();
    expect(trigger().getAttribute("aria-describedby")).toBe("own");
  });
});

describe("AnchoredTooltip", () => {
  it("draws the one bubble against an element the caller tracks", async () => {
    const anchor = document.createElement("span");
    document.body.append(anchor);
    view = await mount(
      <AnchoredTooltip anchor={anchor} content={"acme/repo#12\nbuild · Running"} />,
    );
    expect(bubble()?.textContent).toBe("acme/repo#12\nbuild · Running");
  });
});
