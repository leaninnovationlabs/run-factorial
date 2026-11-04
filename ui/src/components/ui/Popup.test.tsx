// @vitest-environment happy-dom
/**
 * The menu button pattern (ARIA APG), on a non-modal layer: the keys move between items, every way
 * out puts the focus somewhere sensible, and an item that cannot run says why instead of acting.
 */

import { act, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { byText, click, type Mounted, mount, pointerDown, press, settle } from "../../testDom";
import { ConfirmDialog } from "./dialog";
import { clearFlags, FlagGroup } from "./Flag";
import { DropdownMenu, Popup } from "./Popup";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
  clearFlags();
});

const menu = () => document.querySelector('[role="menu"]');
const items = () => [...document.querySelectorAll<HTMLElement>('[role="menuitem"]')];
const trigger = () => byText(document, "button", "Actions") as HTMLButtonElement;

async function opened(onRename = vi.fn()) {
  view = await mount(
    <>
      <button type="button">Elsewhere</button>
      <DropdownMenu
        trigger={{ label: "Actions" }}
        items={[
          { label: "Rename", onClick: onRename },
          {
            group: "Danger",
            items: [
              { label: "Archive", onClick: vi.fn() },
              { label: "Delete", onClick: vi.fn(), unavailable: "Pause the project first." },
            ],
          },
        ]}
      />
      <FlagGroup />
    </>,
  );
  trigger().focus();
  await click(trigger());
  return onRename;
}

describe("DropdownMenu", () => {
  it("opens from its button, says so, and puts the focus on the first item", async () => {
    await opened();
    expect(menu()).not.toBeNull();
    expect(trigger().getAttribute("aria-expanded")).toBe("true");
    expect(trigger().getAttribute("aria-controls")).toBe(menu()?.id);
    expect(document.activeElement).toBe(items()[0]);
  });

  it("moves between items with the arrow keys, wrapping, and jumps with Home and End", async () => {
    await opened();
    const at = () => items().indexOf(document.activeElement as HTMLElement);
    await press("ArrowDown", document.activeElement ?? document);
    expect(at()).toBe(1);
    await press("End", document.activeElement ?? document);
    expect(at()).toBe(2);
    await press("ArrowDown", document.activeElement ?? document);
    expect(at()).toBe(0);
    await press("ArrowUp", document.activeElement ?? document);
    expect(at()).toBe(2);
    await press("Home", document.activeElement ?? document);
    expect(at()).toBe(0);
  });

  it("closes on Escape and gives the focus back to its button", async () => {
    await opened();
    await press("Escape", document.activeElement ?? document);
    expect(menu()).toBeNull();
    expect(trigger().getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(trigger());
  });

  it("closes on Tab from its button, so the Tab carries on from where the menu was opened", async () => {
    await opened();
    await press("Tab", document.activeElement ?? document);
    expect(menu()).toBeNull();
    expect(document.activeElement).toBe(trigger());
  });

  it("closes on a press outside it, leaving the focus where the press put it", async () => {
    await opened();
    await pointerDown(byText(document, "button", "Elsewhere") as HTMLElement);
    expect(menu()).toBeNull();
  });

  it("stays open for a press inside it", async () => {
    await opened();
    await pointerDown(items()[1] as HTMLElement);
    expect(menu()).not.toBeNull();
  });

  it("runs a chosen item and hands the focus back to its button first", async () => {
    const onRename = await opened();
    await click(items()[0]);
    expect(onRename).toHaveBeenCalledOnce();
    expect(menu()).toBeNull();
    expect(document.activeElement).toBe(trigger());
  });

  it("explains an item that cannot run, in the menu and in a flag, instead of acting", async () => {
    await opened();
    const remove = items()[2] as HTMLElement;
    expect(remove.textContent).toContain("Pause the project first.");
    await click(remove);
    await settle();
    const flag = document.querySelector('[data-appearance="info"]');
    expect(flag?.textContent).toContain("Pause the project first.");
  });

  it("is not modal: the page behind it stays live", async () => {
    await opened();
    expect(document.getElementById("root")?.inert).toBe(false);
  });
});

function DeleteMenu() {
  const [confirming, setConfirming] = useState(false);
  return (
    <>
      <DropdownMenu
        trigger={{ label: "More actions", icon: "more", iconOnly: true }}
        items={[{ label: "Delete runtime", onClick: () => setConfirming(true) }]}
      />
      {confirming && (
        <ConfirmDialog
          title="Delete runtime codex-team?"
          consequence="This removes it."
          confirmLabel="Delete runtime"
          onConfirm={() => setConfirming(false)}
          onCancel={() => setConfirming(false)}
        />
      )}
    </>
  );
}

describe("a submenu", () => {
  const item = (name: string) =>
    [...document.querySelectorAll<HTMLElement>('[role^="menuitem"]')].find(
      (el) => el.textContent?.trim() === name,
    ) as HTMLElement;

  async function nested(onDark = vi.fn()) {
    view = await mount(
      <DropdownMenu
        trigger={{ label: "Account" }}
        items={[
          {
            label: "Settings",
            submenu: [
              { label: "Token", onClick: vi.fn() },
              {
                label: "Theme",
                submenu: [
                  { label: "Light", onClick: vi.fn(), isSelected: true },
                  { label: "Dark", onClick: onDark, isSelected: false },
                ],
              },
            ],
          },
          { label: "Sign out", onClick: vi.fn() },
        ]}
      />,
    );
    const button = byText(document, "button", "Account") as HTMLButtonElement;
    button.focus();
    await click(button);
    return { button, onDark };
  }

  it("opens on the right arrow with the focus on its first item, and the arrows stay inside it", async () => {
    await nested();
    expect(document.activeElement).toBe(item("Settings"));
    await press("ArrowRight", document.activeElement ?? document);
    expect(item("Settings").getAttribute("aria-expanded")).toBe("true");
    expect(document.activeElement).toBe(item("Token"));
    await press("ArrowDown", document.activeElement ?? document);
    expect(document.activeElement).toBe(item("Theme"));
    await press("ArrowDown", document.activeElement ?? document);
    expect(document.activeElement).toBe(item("Token"));
  });

  it("closes alone on Escape or the left arrow, back on the item that opened it", async () => {
    await nested();
    await press("ArrowRight", document.activeElement ?? document);
    await press("Escape", document.activeElement ?? document);
    expect(menu()).not.toBeNull();
    expect(item("Token")).toBeUndefined();
    expect(document.activeElement).toBe(item("Settings"));
    await press("ArrowRight", document.activeElement ?? document);
    await press("ArrowLeft", document.activeElement ?? document);
    expect(item("Token")).toBeUndefined();
    expect(document.activeElement).toBe(item("Settings"));
  });

  it("runs an item two levels down and closes the whole menu, the focus on its button", async () => {
    const { button, onDark } = await nested();
    await click(item("Settings"));
    await click(item("Theme"));
    await click(item("Dark"));
    expect(onDark).toHaveBeenCalledOnce();
    expect(menu()).toBeNull();
    expect(document.activeElement).toBe(button);
  });
});

describe("a dialog opened from a menu", () => {
  it("returns the focus to the menu's button when it closes, not to the page", async () => {
    view = await mount(<DeleteMenu />);
    const more = document.querySelector('[aria-label="More actions"]') as HTMLButtonElement;
    more.focus();
    await click(more);
    await click(byText(document, '[role="menuitem"]', "Delete runtime"));
    await settle(20);
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    await click(byText(document, "button", "Cancel"));
    await settle(260);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(more);
  });
});

describe("Popup", () => {
  it("focuses itself when nothing inside asks for the focus, and closes on focus leaving", async () => {
    function Explain() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <Popup
            isOpen={open}
            onClose={() => setOpen(false)}
            label="Why"
            trigger={(props) => (
              <button type="button" {...props} onClick={() => setOpen(true)}>
                Why?
              </button>
            )}
          >
            <p>Because.</p>
          </Popup>
          <button type="button">Next</button>
        </>
      );
    }
    view = await mount(<Explain />);
    await click(byText(document, "button", "Why?"));
    const dialog = document.querySelector('[role="dialog"]') as HTMLElement;
    expect(dialog.getAttribute("aria-label")).toBe("Why");
    expect(document.activeElement).toBe(dialog);
    await act(async () => (byText(document, "button", "Next") as HTMLElement).focus());
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });
});

describe("the popup surface", () => {
  it("stacks above a drawer or dialog, since it can be opened from one", async () => {
    await opened();
    expect(menu()?.classList).toContain("z-popup");
  });

  it("follows its button when what holds the button scrolls", async () => {
    // The window never scrolls; `main` does, and the surface is fixed to the viewport.
    view = await mount(
      <>
        <div id="scroller" />
        <DropdownMenu
          trigger={{ label: "Actions" }}
          items={[{ label: "Rename", onClick: vi.fn() }]}
        />
      </>,
    );
    let top = 100;
    trigger().getBoundingClientRect = () =>
      ({
        top,
        left: 40,
        width: 80,
        height: 24,
        right: 120,
        bottom: top + 24,
        x: 40,
        y: top,
      }) as DOMRect;
    await click(trigger());
    const before = menu()?.getAttribute("style");
    top = 60;
    await act(async () => {
      document.getElementById("scroller")?.dispatchEvent(new Event("scroll"));
    });
    expect(menu()?.getAttribute("style")).not.toBe(before);
    expect((menu() as HTMLElement | null)?.style.top).toBe("90px");
  });
});
