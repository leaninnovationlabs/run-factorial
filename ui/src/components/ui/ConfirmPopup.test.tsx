// @vitest-environment happy-dom
/**
 * The confirmation for what can be undone (decision 50): the first press opens a popup over the
 * page saying what the second press spends, and every way out puts the focus back on the button.
 */

import { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { byText, click, type Mounted, mount, press, settle } from "../../testDom";
import { ConfirmPopup } from "./ConfirmPopup";
import { clearFlags, FlagGroup } from "./Flag";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
  clearFlags();
});

const CONSEQUENCE = "This exits any runs in flight. The ticket can come back with Re-enter.";

function exit(overrides: Partial<Parameters<typeof ConfirmPopup>[0]> = {}) {
  return (
    <>
      <ConfirmPopup
        label="Exit"
        title="Exit this ticket?"
        confirmLabel="Exit ticket"
        consequence={CONSEQUENCE}
        resetKey="running"
        onConfirm={() => {}}
        {...overrides}
      />
      <FlagGroup />
    </>
  );
}

const trigger = () => byText(document, "button", "Exit") as HTMLButtonElement;
const popup = () => document.querySelector<HTMLElement>('[role="dialog"]');

async function opened(onConfirm = vi.fn()) {
  view = await mount(exit({ onConfirm }));
  trigger().focus();
  await click(trigger());
  await settle();
  return onConfirm;
}

describe("ConfirmPopup", () => {
  it("opens on the first press: a popup named by its question, saying what the second spends", async () => {
    const onConfirm = await opened();
    expect(onConfirm).not.toHaveBeenCalled();
    expect(popup()?.getAttribute("aria-label")).toBe("Exit this ticket?");
    expect(popup()?.textContent).toContain(CONSEQUENCE);
    expect(trigger().getAttribute("aria-haspopup")).toBe("dialog");
    expect(trigger().getAttribute("aria-expanded")).toBe("true");
    expect(trigger().getAttribute("aria-controls")).toBe(popup()?.id);
  });

  it("floats over the page instead of opening inside the row, so nothing beside it moves", async () => {
    await opened();
    expect(popup()).not.toBeNull();
    expect(view?.container.contains(popup())).toBe(false);
  });

  it("puts the focus on Cancel, the safe answer, and says the consequence aloud", async () => {
    await opened();
    expect(document.activeElement?.textContent).toBe("Cancel");
    expect(document.querySelector('[aria-live="polite"]')?.textContent).toBe(CONSEQUENCE);
  });

  it("closes on Escape, spends the key, and hands the focus back to the button", async () => {
    await opened();
    const escapeKey = new KeyboardEvent("keydown", {
      key: "Escape",
      bubbles: true,
      cancelable: true,
    });
    await act(async () => {
      document.activeElement?.dispatchEvent(escapeKey);
    });
    await settle(40);
    expect(popup()).toBeNull();
    expect(escapeKey.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(trigger());
  });

  it("closes on Cancel and hands the focus back to the button", async () => {
    await opened();
    await click(byText(document, "button", "Cancel"));
    await settle(40);
    expect(popup()).toBeNull();
    expect(document.activeElement).toBe(trigger());
  });

  it("closes on a press anywhere else, and runs nothing", async () => {
    const onConfirm = await opened();
    await act(async () => {
      document.body.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    });
    await settle();
    expect(popup()).toBeNull();
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("runs the action on the second press, and returns the focus to the button", async () => {
    const onConfirm = await opened();
    await click(byText(document, "button", "Exit ticket"));
    await settle(40);
    expect(onConfirm).toHaveBeenCalledOnce();
    expect(popup()).toBeNull();
    expect(document.activeElement).toBe(trigger());
  });

  it("tells the caller of Cancel and Escape only, so a stray press outside keeps a typed note", async () => {
    const onDismiss = vi.fn();
    view = await mount(exit({ onDismiss }));
    const open = async () => {
      await click(trigger());
      await settle();
    };

    await open();
    await click(byText(document, "button", "Cancel"));
    await settle(40);
    await open();
    await act(async () => {
      document.activeElement?.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }),
      );
    });
    await settle(40);
    await open();
    await act(async () => {
      document.body.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    });
    await settle();
    await open();
    await click(trigger());
    await settle(40);
    await open();
    await click(byText(document, "button", "Exit ticket"));
    await settle(40);
    expect(onDismiss).toHaveBeenCalledTimes(2);
  });

  it("names a short label in full, the words shown being part of the name", async () => {
    view = await mount(exit({ name: "Exit ticket" }));
    expect(trigger().getAttribute("aria-label")).toBe("Exit ticket");
  });

  it("will not open while it has a reason not to act", async () => {
    view = await mount(exit({ disabledReason: "Checking issue…" }));
    await click(trigger());
    await press("Enter", trigger());
    expect(popup()).toBeNull();
    expect(trigger().getAttribute("aria-disabled")).toBe("true");
  });

  it("drops a confirmation given for a state the ticket has since left", async () => {
    await opened();
    await view?.rerender(exit({ resetKey: "waiting_gate" }));
    expect(popup()).toBeNull();
  });
});
