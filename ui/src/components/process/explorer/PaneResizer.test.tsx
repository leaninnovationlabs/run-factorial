// @vitest-environment happy-dom
/**
 * The file panel's right border, dragged or moved with the arrow keys: it reports the width asked
 * for, and the editor clamps it.
 */

import { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type Mounted, mount, press } from "../../../testDom";
import { PaneResizer } from "./PaneResizer";

let view: Mounted | null = null;
afterEach(async () => {
  await view?.unmount();
  view = null;
});

const handle = () => document.querySelector<HTMLElement>('[role="separator"]');
const pointer = async (type: string, clientX: number) => {
  await act(async () => {
    handle()?.dispatchEvent(
      new MouseEvent(type, { bubbles: true, cancelable: true, clientX, button: 0 }),
    );
  });
};

async function drawn(onResize = vi.fn(), onDone = vi.fn()) {
  view = await mount(
    <PaneResizer
      width={288}
      min={192}
      max={400}
      left={() => 100}
      onResize={onResize}
      onDone={onDone}
    />,
  );
  return { onResize, onDone };
}

describe("PaneResizer", () => {
  it("is a named separator that says how wide the panel is and may be", async () => {
    await drawn();
    expect(handle()?.getAttribute("aria-label")).toBe("Resize the file panel");
    expect(handle()?.getAttribute("aria-orientation")).toBe("vertical");
    expect(handle()?.getAttribute("aria-valuenow")).toBe("288");
    expect(handle()?.getAttribute("aria-valuemin")).toBe("192");
    expect(handle()?.getAttribute("aria-valuemax")).toBe("400");
    expect(handle()?.tabIndex).toBe(0);
  });

  it("follows the pointer while it is held, measured from the panel's left edge", async () => {
    const { onResize, onDone } = await drawn();
    await pointer("pointermove", 500);
    expect(onResize).not.toHaveBeenCalled();
    await pointer("pointerdown", 388);
    await pointer("pointermove", 450);
    expect(onResize).toHaveBeenLastCalledWith(350);
    await pointer("pointerup", 450);
    expect(onDone).toHaveBeenCalledTimes(1);
    await pointer("pointermove", 600);
    expect(onResize).toHaveBeenCalledTimes(1);
  });

  it("moves with the arrow keys, and to either end with Home and End", async () => {
    const { onResize } = await drawn();
    handle()?.focus();
    await press("ArrowRight", handle() ?? document);
    expect(onResize).toHaveBeenLastCalledWith(304);
    await press("ArrowLeft", handle() ?? document);
    expect(onResize).toHaveBeenLastCalledWith(272);
    await press("Home", handle() ?? document);
    expect(onResize).toHaveBeenLastCalledWith(192);
    await press("End", handle() ?? document);
    expect(onResize).toHaveBeenLastCalledWith(400);
  });
});
