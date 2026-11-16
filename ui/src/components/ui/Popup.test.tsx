// @vitest-environment happy-dom
/**
 * The menu button pattern (ARIA APG), on a non-modal layer: the keys move between items, every way
 * out puts the focus somewhere sensible, and an item that cannot run says why instead of acting.
 */

import { act, useRef, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { byText, click, type Mounted, mount, pointerDown, press, settle } from "../../testDom";
import { ConfirmDialog } from "./dialog";
import { clearFlags, FlagGroup } from "./Flag";
import { ContextMenu, DropdownMenu, Popup, PopupAt } from "./Popup";

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

  it("hangs from the anchor it is given rather than its button, which keeps the focus", async () => {
    // The top bar's place: the menu hangs under the name and its chevron, not the chevron alone.
    function Place() {
      const anchor = useRef<HTMLDivElement | null>(null);
      return (
        <div ref={anchor} data-anchor>
          <DropdownMenu
            anchorRef={anchor}
            placement="bottom-start"
            trigger={{ label: "Actions" }}
            items={[{ label: "Rename", onClick: vi.fn() }]}
          />
        </div>
      );
    }
    view = await mount(<Place />);
    const box = (left: number, width: number) => () =>
      ({
        top: 10,
        left,
        width,
        height: 32,
        right: left + width,
        bottom: 42,
        x: left,
        y: 10,
      }) as DOMRect;
    (document.querySelector("[data-anchor]") as HTMLElement).getBoundingClientRect = box(40, 160);
    trigger().getBoundingClientRect = box(176, 24);
    trigger().focus();
    await click(trigger());
    expect((menu() as HTMLElement).style.left).toBe("40px");
    expect((menu() as HTMLElement).style.top).toBe("48px");
    await press("Escape", menu() as HTMLElement);
    expect(document.activeElement).toBe(trigger());
  });
});

describe("a menu item with an icon", () => {
  it("draws the icon before its label, and none where none is given", async () => {
    view = await mount(
      <DropdownMenu
        trigger={{ label: "Actions" }}
        items={[
          {
            group: "Add new",
            items: [
              { label: "Agent", icon: "agent", onClick: vi.fn() },
              { label: "Plain", onClick: vi.fn() },
            ],
          },
        ]}
      />,
    );
    await click(trigger());
    const [agent, plain] = items();
    expect(agent?.querySelector("svg")?.getAttribute("data-glyph")).toBe("agent");
    expect(agent?.textContent).toBe("Agent");
    expect(plain?.querySelector("svg")).toBeNull();
    expect(document.querySelector('[role="group"]')?.getAttribute("aria-label")).toBe("Add new");
  });
});

describe("PopupAt", () => {
  function Prompt({ onClose }: { onClose: () => void }) {
    return (
      <>
        <button type="button">elsewhere</button>
        <PopupAt at={{ x: 40, y: 60 }} label="New agent" onClose={onClose}>
          {/* biome-ignore lint/a11y/noAutofocus: the prompt exists to take one name */}
          <input aria-label="name" autoFocus />
        </PopupAt>
      </>
    );
  }
  const dialog = () => document.querySelector<HTMLElement>('[role="dialog"]');

  it("opens as a named dialog under the point, and leaves the focus on the field that took it", async () => {
    view = await mount(<Prompt onClose={vi.fn()} />);
    await settle();
    expect(dialog()?.getAttribute("aria-label")).toBe("New agent");
    expect(dialog()?.style.left).toBe("40px");
    expect(dialog()?.style.top).toBe("66px");
    expect(document.activeElement?.getAttribute("aria-label")).toBe("name");
  });

  it("closes on Escape and on a press outside it", async () => {
    const onClose = vi.fn();
    view = await mount(<Prompt onClose={onClose} />);
    await settle();
    await press("Escape");
    expect(onClose).toHaveBeenCalledTimes(1);
    await pointerDown(document.body);
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("draws nothing without a point", async () => {
    view = await mount(
      <PopupAt at={null} label="New agent" onClose={vi.fn()}>
        <span>inside</span>
      </PopupAt>,
    );
    expect(dialog()).toBeNull();
  });
});

describe("ContextMenu", () => {
  function Row({ onRename }: { onRename: () => void }) {
    const [at, setAt] = useState<{ x: number; y: number } | null>(null);
    return (
      <>
        <button
          type="button"
          onContextMenu={(event) => {
            event.preventDefault();
            setAt({ x: event.clientX, y: event.clientY });
          }}
        >
          agents
        </button>
        <ContextMenu
          at={at}
          label="Actions for agents"
          items={[
            { label: "New agent", onClick: onRename },
            { label: "Delete folder", onClick: vi.fn() },
          ]}
          onClose={() => setAt(null)}
        />
      </>
    );
  }
  const row = () => byText(document, "button", "agents") as HTMLButtonElement;

  async function rightClicked(onRename = vi.fn()) {
    view = await mount(<Row onRename={onRename} />);
    row().focus();
    await act(async () => {
      row().dispatchEvent(
        new MouseEvent("contextmenu", {
          bubbles: true,
          cancelable: true,
          clientX: 40,
          clientY: 60,
        }),
      );
    });
    await settle();
    return onRename;
  }

  it("is closed until it is given a point", async () => {
    view = await mount(<Row onRename={vi.fn()} />);
    expect(menu()).toBeNull();
  });

  it("opens as a named menu with the focus on its first item", async () => {
    await rightClicked();
    expect(menu()?.getAttribute("aria-label")).toBe("Actions for agents");
    // Under the point of the right-click (40, 60), by the popup's own gap.
    expect((menu() as HTMLElement).style.left).toBe("40px");
    expect((menu() as HTMLElement).style.top).toBe("66px");
    expect(items().map((item) => item.textContent)).toEqual(["New agent", "Delete folder"]);
    expect(document.activeElement).toBe(items()[0]);
  });

  it("moves between its items with the arrow keys, as a menu does", async () => {
    await rightClicked();
    await press("ArrowDown", document.activeElement ?? document);
    expect(document.activeElement).toBe(items()[1]);
  });

  it("closes on Escape and gives the focus back to what had it", async () => {
    await rightClicked();
    await press("Escape");
    expect(menu()).toBeNull();
    expect(document.activeElement).toBe(row());
  });

  it("closes on a press outside it", async () => {
    await rightClicked();
    await pointerDown(document.body);
    expect(menu()).toBeNull();
  });

  it("runs a chosen item and closes", async () => {
    const onRename = await rightClicked();
    await click(items()[0]);
    expect(onRename).toHaveBeenCalledTimes(1);
    expect(menu()).toBeNull();
  });
});
