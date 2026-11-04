// @vitest-environment happy-dom
/**
 * The confirmation for what cannot be undone (decision 50), on the overlay stack: the focus
 * starts on the safe answer, the page behind goes inert, and Escape reaches only the top layer —
 * so a dialog opened from a drawer closes alone and leaves the drawer open.
 */

import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { byText, click, type Mounted, mount, press, settle } from "../../testDom";
import { ConfirmDialog, Drawer } from "./dialog";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
});

const dialogs = () => [...document.querySelectorAll('[role="dialog"]')];
const root = () => document.getElementById("root");

function Forget({ onConfirm = vi.fn(), error }: { onConfirm?: () => void; error?: unknown }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Remove
      </button>
      {open && (
        <ConfirmDialog
          title="Forget OPENAI_API_KEY?"
          consequence="Its value cannot be read back."
          confirmLabel="Remove credential"
          error={error}
          onConfirm={onConfirm}
          onCancel={() => setOpen(false)}
        />
      )}
    </>
  );
}

async function opened(node = <Forget />) {
  view = await mount(node);
  const trigger = byText(document, "button", "Remove") as HTMLButtonElement;
  trigger.focus();
  await click(trigger);
  await settle(20);
  return trigger;
}

describe("ConfirmDialog", () => {
  it("asks its question as the dialog's name, and starts on Cancel", async () => {
    await opened();
    const dialog = dialogs()[0];
    const title = document.getElementById(dialog?.getAttribute("aria-labelledby") ?? "");
    expect(title?.tagName).toBe("H1");
    expect(title?.textContent).toBe("Forget OPENAI_API_KEY?");
    expect(document.activeElement?.textContent).toBe("Cancel");
  });

  it("makes the page inert while it is open, and gives it back after", async () => {
    const trigger = await opened();
    expect(root()?.inert).toBe(true);
    await click(byText(document, "button", "Cancel"));
    await settle(260);
    expect(dialogs()).toHaveLength(0);
    expect(root()?.inert).toBe(false);
    expect(document.activeElement).toBe(trigger);
  });

  it("cancels on Escape", async () => {
    await opened();
    await press("Escape");
    await settle(260);
    expect(dialogs()).toHaveLength(0);
  });

  it("confirms with the button that says what it does, and shows a failure in place", async () => {
    const onConfirm = vi.fn();
    await opened(<Forget onConfirm={onConfirm} error={new Error("The runtime is in use.")} />);
    await click(byText(document, "button", "Remove credential"));
    expect(onConfirm).toHaveBeenCalledOnce();
    expect(dialogs()[0]?.textContent).toContain("The runtime is in use.");
  });
});

function EditorWithDelete() {
  const [editing, setEditing] = useState(true);
  const [confirming, setConfirming] = useState(false);
  return editing ? (
    <Drawer title="Process" onClose={() => setEditing(false)}>
      <button type="button" onClick={() => setConfirming(true)}>
        Delete step
      </button>
      {confirming && (
        <ConfirmDialog
          title="Delete step build?"
          consequence="Tickets pinned to it keep it."
          confirmLabel="Delete step"
          onConfirm={() => setConfirming(false)}
          onCancel={() => setConfirming(false)}
        />
      )}
    </Drawer>
  ) : null;
}

describe("a dialog over a drawer", () => {
  it("takes Escape first and closes alone, leaving the drawer open and live again", async () => {
    view = await mount(<EditorWithDelete />);
    await settle(20);
    const remove = byText(document, "button", "Delete step") as HTMLButtonElement;
    remove.focus();
    await click(remove);
    await settle(20);
    expect(dialogs()).toHaveLength(2);
    const drawerLayer = dialogs()[0]?.parentElement;
    // The drawer goes inert under the dialog, as the page does under the drawer.
    expect(drawerLayer?.inert).toBe(true);
    await press("Escape");
    await settle(260);
    expect(dialogs()).toHaveLength(1);
    expect(dialogs()[0]?.textContent).toContain("Process");
    expect(drawerLayer?.inert).toBe(false);
    expect(root()?.inert).toBe(true);
    expect(document.activeElement).toBe(remove);
  });
});
