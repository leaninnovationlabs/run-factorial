// @vitest-environment happy-dom
/**
 * The drawer as a dialog: named by its title, holding the focus while it is open, and giving it
 * back to whatever opened it.
 */

import { useRef, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { byText, click, type Mounted, mount, press, settle } from "../../testDom";
import { Drawer, type DrawerHandle } from "./dialog";

function Harness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open settings
      </button>
      {open && (
        <Drawer title="Board settings" onClose={() => setOpen(false)}>
          <p>Body</p>
        </Drawer>
      )}
    </>
  );
}

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
});

async function opened() {
  view = await mount(<Harness />);
  const trigger = byText(document, "button", "Open settings");
  trigger?.focus();
  await click(trigger);
  await settle(20);
  return trigger;
}

describe("Drawer", () => {
  it("is a modal dialog named by its title", async () => {
    await opened();
    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog?.getAttribute("aria-modal")).toBe("true");
    const labelledBy = dialog?.getAttribute("aria-labelledby") ?? "";
    expect(labelledBy).not.toBe("");
    expect(document.getElementById(labelledBy)?.textContent).toBe("Board settings");
  });

  it("moves the focus to Close when it opens", async () => {
    await opened();
    const active = document.activeElement as HTMLElement | null;
    expect(active?.getAttribute("aria-label") ?? active?.textContent).toBe("Close");
  });

  it("makes the page behind it inert, and keeps the backdrop out of the tab order", async () => {
    await opened();
    expect(document.getElementById("root")?.inert).toBe(true);
    const backdrop = document.querySelector("[data-blanket]");
    expect(backdrop?.getAttribute("aria-hidden")).toBe("true");
    expect(backdrop?.tagName).not.toBe("BUTTON");
  });

  it("puts its actions beside Close, where a reader looks for a way out", async () => {
    view = await mount(
      <Drawer title="Transcript" actions={<button type="button">Copy</button>} onClose={() => {}}>
        <p>Body</p>
      </Drawer>,
    );
    const close = document.querySelector('[aria-label="Close"]');
    expect(close?.parentElement?.textContent).toBe("Copy");
  });

  it("closes on Escape and gives the focus back to what opened it", async () => {
    const trigger = await opened();
    expect(document.activeElement).not.toBe(trigger);
    await press("Escape");
    await settle(260);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.getElementById("root")?.inert).toBe(false);
    expect(document.activeElement).toBe(trigger);
  });

  it("slides out when what it holds closes it, then calls onClose", async () => {
    const closed = vi.fn();
    function Holder() {
      const drawer = useRef<DrawerHandle>(null);
      return (
        <Drawer title="Output" handle={drawer} onClose={closed}>
          <button type="button" onClick={() => drawer.current?.close()}>
            Send
          </button>
        </Drawer>
      );
    }
    view = await mount(<Holder />);
    await settle(20);
    await click(byText(document, "button", "Send"));
    expect(document.querySelector('[role="dialog"]')?.className).toContain("translate-x-full");
    expect(closed).not.toHaveBeenCalled();
    await settle(260);
    expect(closed).toHaveBeenCalledTimes(1);
  });

  it("gives the focus back without scrolling the page to what opened it", async () => {
    // What opened it may have moved while the page was covered; the page stays where it was.
    const trigger = await opened();
    const focus = vi.spyOn(trigger as HTMLElement, "focus");
    await press("Escape");
    await settle(260);
    expect(focus).toHaveBeenCalledWith({ preventScroll: true });
  });
});
