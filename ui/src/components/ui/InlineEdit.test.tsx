// @vitest-environment happy-dom
/**
 * Inline edit (ADS): read as text until pressed, then a field where Enter saves and Escape
 * cancels — and after either, the focus is back on the value it started from.
 */

import { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { byText, click, type Mounted, mount, settle, type } from "../../testDom";
import { InlineEditText } from "./InlineEdit";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
});

const readView = () =>
  document.querySelector('button[aria-label^="Edit name"]') as HTMLButtonElement;
const input = () => document.querySelector("input") as HTMLInputElement;

async function keydown(key: string) {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
  await act(async () => {
    input().dispatchEvent(event);
  });
  return event;
}

async function editing(onConfirm = vi.fn()) {
  view = await mount(
    <InlineEditText
      label="Name"
      value="codex team"
      validate={(value) => (value.trim() === "" ? "A runtime needs a name." : null)}
      onConfirm={onConfirm}
    />,
  );
  readView().focus();
  await click(readView());
  return onConfirm;
}

describe("InlineEditText", () => {
  it("reads as the value, named for what pressing it edits", async () => {
    view = await mount(<InlineEditText label="Name" value="codex team" onConfirm={vi.fn()} />);
    expect(readView().getAttribute("aria-label")).toBe("Edit name: codex team");
    expect(input()).toBeNull();
  });

  it("opens a field holding the value, with the focus in it", async () => {
    await editing();
    expect(input().value).toBe("codex team");
    expect(document.activeElement).toBe(input());
    expect(document.getElementById(input().id)).toBe(input());
    expect(byText(document, "label", "Name")?.getAttribute("for")).toBe(input().id);
  });

  it("saves a changed value on Enter and goes back to reading it, focus and all", async () => {
    const onConfirm = await editing();
    await type(input(), "codex platform");
    await keydown("Enter");
    await settle(40);
    expect(onConfirm).toHaveBeenCalledWith("codex platform");
    expect(input()).toBeNull();
    expect(document.activeElement).toBe(readView());
  });

  it("sends nothing when the value did not change", async () => {
    const onConfirm = await editing();
    await keydown("Enter");
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("cancels on Escape, spends the key, and puts the focus back on the value", async () => {
    const onConfirm = await editing();
    await type(input(), "something else");
    const escapeKey = await keydown("Escape");
    await settle(40);
    expect(escapeKey.defaultPrevented).toBe(true);
    expect(onConfirm).not.toHaveBeenCalled();
    expect(input()).toBeNull();
    expect(document.activeElement).toBe(readView());
    expect(readView().getAttribute("aria-label")).toBe("Edit name: codex team");
  });

  it("refuses a value that fails validation, says why beside the field, and stays open", async () => {
    const onConfirm = await editing();
    await type(input(), "   ");
    // Not before an attempt: nobody is told off for a field they are still typing in.
    expect(input().hasAttribute("aria-invalid")).toBe(false);
    await keydown("Enter");
    expect(onConfirm).not.toHaveBeenCalled();
    expect(input().getAttribute("aria-invalid")).toBe("true");
    const message = document.getElementById(input().getAttribute("aria-describedby") ?? "");
    expect(message?.textContent).toBe("A runtime needs a name.");
  });

  it("shows a failed save beside the value as an alert", async () => {
    view = await mount(
      <InlineEditText
        label="Name"
        value="codex team"
        error={new Error("That name is taken.")}
        onConfirm={vi.fn()}
      />,
    );
    expect(document.querySelector('[role="alert"]')?.textContent).toBe("That name is taken.");
  });
});
