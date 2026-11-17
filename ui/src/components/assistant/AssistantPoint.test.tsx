// @vitest-environment happy-dom
/**
 * Pointing at something: a right-click anywhere on the dashboard offers to take what was under
 * the pointer into a conversation, where it waits as a chip beside what the person writes.
 */

import { act } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { api } from "../../api/client";
import { byText, click, type Mounted, mount, press, settle, type } from "../../testDom";
import { FlagGroup } from "../ui";
import { pushLayer } from "../ui/layers";
import { useAssistant, usePointEntries } from "./AssistantProvider";
import { chatItem, mountInAssistant, stubChatApi } from "./testFakes";

let view: Mounted | null = null;

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
  window.localStorage.clear();
});

afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.restoreAllMocks();
});

const ANSWERED = [
  chatItem(1, 1, "user_text", { text: "which tickets are blocked?", page: "/overview" }),
  chatItem(2, 2, "assistant_text", { text: "Two are **blocked**." }),
];
const ROW = "#212 · Checkout total ignores the discount code · Blocked";
const POINTED = {
  label: ROW,
  text: ROW,
  page: "/projects/9041/tickets",
  href: "/tickets/212",
};

const drawer = () => document.querySelector<HTMLElement>('[role="dialog"][aria-modal="true"]');
const mini = () => document.querySelector<HTMLElement>("[data-assistant-mini]");
const menu = () => document.querySelector<HTMLElement>('[role="menu"]');
const entries = () =>
  [...(menu()?.querySelectorAll('[role="menuitem"]') ?? [])].map((item) => item.textContent);
const chips = (scope: ParentNode | null) =>
  [...(scope?.querySelectorAll("[data-pointed-chip]") ?? [])].map(
    (chip) => chip.querySelector("button")?.textContent,
  );
const at = (selector: string) => view?.container.querySelector(selector) as HTMLElement;

async function until(found: () => unknown) {
  for (let tries = 0; tries < 30 && !found(); tries += 1) await settle();
}

function Page() {
  const { openDrawer } = useAssistant();
  return (
    <>
      <button type="button" onClick={openDrawer}>
        Open the Drawer
      </button>
      <table>
        <tbody>
          <tr data-row>
            <td>#212</td>
            <td>
              <a href="/tickets/212">Checkout total ignores the discount code</a>
            </td>
            <td data-cell>Blocked</td>
          </tr>
        </tbody>
      </table>
      <input data-plain aria-label="A plain field" />
      <FlagGroup />
      {/* biome-ignore lint/a11y/noStaticElementInteractions: a place that has a menu of its own */}
      <div data-own onContextMenu={(event) => event.preventDefault()}>
        A place with its own menu
      </div>
    </>
  );
}

async function onPage(options: { permissions?: string[] } = {}) {
  const chat = stubChatApi({ items: ANSWERED });
  const send = vi.spyOn(api, "sendAssistantMessage").mockResolvedValue({ turn_id: 4 });
  const mounted = await mountInAssistant(<Page />, { at: "/projects/9041/tickets", ...options });
  view = mounted.view;
  return { ...mounted, ...chat, send };
}

/** A right-click as the browser sends it; true when the page took the menu from the browser. */
async function rightClick(target: Element, init: MouseEventInit = {}): Promise<boolean> {
  const event = new MouseEvent("contextmenu", {
    bubbles: true,
    cancelable: true,
    button: 2,
    clientX: 120,
    clientY: 90,
    ...init,
  });
  await act(async () => {
    target.dispatchEvent(event);
  });
  return event.defaultPrevented;
}

async function choose(entry: string) {
  await click(byText(menu() ?? document.body, '[role="menuitem"]', entry));
}

async function pointAndAdd() {
  await rightClick(at("[data-cell]"));
  await choose("Add to current conversation");
  await until(() => drawer()?.textContent?.includes("which tickets are blocked?"));
}

describe("the right-click menu", () => {
  test("offers the two ways into a conversation under the assistant's name, and rings what was pointed at", async () => {
    await onPage();
    expect(await rightClick(at("[data-cell]"))).toBe(true);
    expect(menu()?.textContent).toContain("Assistant");
    expect(entries()).toEqual(["Start new conversation", "Add to current conversation"]);
    expect(at("[data-row]").hasAttribute("data-assist-shown")).toBe(true);
    await press("Escape", document);
    expect(menu()).toBeNull();
    expect(at("[data-row]").hasAttribute("data-assist-shown")).toBe(false);
  });

  test("Shift, a plain field and a place with its own menu keep the menu they had", async () => {
    await onPage();
    expect(await rightClick(at("[data-cell]"), { shiftKey: true })).toBe(false);
    expect(await rightClick(at("[data-plain]"))).toBe(false);
    await rightClick(at("[data-own]"));
    expect(menu()).toBeNull();
  });

  test("a reader who may not use the assistant keeps the browser's menu", async () => {
    await onPage({ permissions: [] });
    expect(await rightClick(at("[data-cell]"))).toBe(false);
    expect(menu()).toBeNull();
  });

  test("the conversation itself keeps the browser's menu", async () => {
    await onPage();
    await pointAndAdd();
    const reply = drawer()?.querySelector('[data-speaker="assistant"]') as HTMLElement;
    expect(await rightClick(reply)).toBe(false);
  });
});

describe("a place with a menu of its own", () => {
  function OwnMenu() {
    const entries = usePointEntries();
    const group = entries(() => ({
      label: "prompt.md",
      text: ".factorial/agents/spec/prompt.md",
      page: null,
      file: ".factorial/agents/spec/prompt.md",
    }));
    const items = group && "group" in group ? group.items : [];
    return (
      <ul data-own-entries>
        {items.map((item) => (
          <li key={item.label}>
            <button type="button" onClick={item.onClick}>
              {item.label}
            </button>
          </li>
        ))}
      </ul>
    );
  }

  test("is handed the assistant's two entries, which point at its thing on the page it is on", async () => {
    stubChatApi({ items: ANSWERED });
    const send = vi.spyOn(api, "sendAssistantMessage").mockResolvedValue({ turn_id: 4 });
    const mounted = await mountInAssistant(<OwnMenu />, { at: "/projects/9041/configuration" });
    view = mounted.view;
    const own = view.container.querySelector("[data-own-entries]");
    expect([...(own?.querySelectorAll("button") ?? [])].map((b) => b.textContent)).toEqual([
      "Start new conversation",
      "Add to current conversation",
    ]);
    await click(byText(own ?? document.body, "button", "Add to current conversation"));
    await until(() => drawer()?.textContent?.includes("which tickets are blocked?"));
    await press("Enter", drawer()?.querySelector("textarea") ?? document);
    await settle();
    expect(send).toHaveBeenCalledExactlyOnceWith(8, {
      text: "",
      page: "/projects/9041/configuration",
      context: [
        {
          label: "prompt.md",
          text: ".factorial/agents/spec/prompt.md",
          page: "/projects/9041/configuration",
          file: ".factorial/agents/spec/prompt.md",
        },
      ],
    });
  });

  test("a name or a text longer than a message takes is cut before it is kept", async () => {
    function Long() {
      const entries = usePointEntries();
      const group = entries(() => ({ label: "n".repeat(200), text: "t".repeat(5000), page: null }));
      const items = group && "group" in group ? group.items : [];
      return (
        <button type="button" data-long onClick={items[1]?.onClick}>
          Add
        </button>
      );
    }
    stubChatApi({ items: ANSWERED });
    const send = vi.spyOn(api, "sendAssistantMessage").mockResolvedValue({ turn_id: 4 });
    const mounted = await mountInAssistant(<Long />, { at: "/projects/9041/configuration" });
    view = mounted.view;
    await click(view.container.querySelector("[data-long]"));
    await until(() => drawer()?.textContent?.includes("which tickets are blocked?"));
    await press("Enter", drawer()?.querySelector("textarea") ?? document);
    await settle();
    const [, body] = send.mock.calls[0] ?? [];
    expect(body?.context?.[0]?.label).toHaveLength(80);
    expect(body?.context?.[0]?.text).toHaveLength(2000);
  });

  test("gets none where there is no assistant around it, or the reader may not use it", async () => {
    view = await mount(<OwnMenu />);
    expect(view.container.querySelectorAll("[data-own-entries] button")).toHaveLength(0);
    await view.unmount();
    stubChatApi();
    const mounted = await mountInAssistant(<OwnMenu />, { permissions: [] });
    view = mounted.view;
    expect(view.container.querySelectorAll("[data-own-entries] button")).toHaveLength(0);
  });
});

describe("a selection made by the right-click itself", () => {
  const select = (element: Element) => {
    const range = document.createRange();
    range.selectNodeContents(element);
    window.getSelection()?.removeAllRanges();
    window.getSelection()?.addRange(range);
  };
  const pressRight = (target: Element) =>
    act(async () => {
      target.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, button: 2 }));
    });

  afterEach(() => window.getSelection()?.removeAllRanges());

  test("is not what was pointed at: some systems select the word under the pointer on the way to the menu", async () => {
    await onPage();
    await pressRight(at("[data-cell]"));
    select(at("[data-cell]"));
    await rightClick(at("[data-cell]"));
    await choose("Add to current conversation");
    await until(() => drawer()?.querySelector("[data-pointed-chip]"));
    expect(chips(drawer())).toEqual([ROW]);
    expect(String(window.getSelection())).toBe("");
  });

  test("but text the person had selected before the click is", async () => {
    await onPage();
    select(at("[data-cell]"));
    await pressRight(at("[data-cell]"));
    await rightClick(at("[data-cell]"));
    await choose("Add to current conversation");
    await until(() => drawer()?.querySelector("[data-pointed-chip]"));
    expect(chips(drawer())).toEqual(['"Blocked"']);
  });
});

describe("what was pointed at", () => {
  test("Add to current conversation opens the Drawer with a chip, and the message carries it", async () => {
    const { send } = await onPage();
    await pointAndAdd();
    expect(chips(drawer())).toEqual([ROW]);
    const field = drawer()?.querySelector("textarea") ?? null;
    expect(document.activeElement).toBe(field);
    await type(field, "Why is this blocked?");
    await press("Enter", field ?? document);
    await settle();
    expect(send).toHaveBeenCalledExactlyOnceWith(8, {
      text: "Why is this blocked?",
      page: "/projects/9041/tickets",
      context: [POINTED],
    });
    expect(chips(drawer())).toEqual([]);
  });

  test("a message can be the chip alone", async () => {
    const { send } = await onPage();
    await pointAndAdd();
    await press("Enter", drawer()?.querySelector("textarea") ?? document);
    await settle();
    expect(send).toHaveBeenCalledExactlyOnceWith(8, {
      text: "",
      page: "/projects/9041/tickets",
      context: [POINTED],
    });
  });

  test("a refused send keeps the chip", async () => {
    const { send } = await onPage();
    send.mockRejectedValue(new Error("no"));
    await pointAndAdd();
    await press("Enter", drawer()?.querySelector("textarea") ?? document);
    await settle();
    expect(chips(drawer())).toEqual([ROW]);
  });

  test("its remove button takes it out", async () => {
    await onPage();
    await pointAndAdd();
    await click(drawer()?.querySelector(`[aria-label="Remove ${ROW}"]`));
    expect(chips(drawer())).toEqual([]);
  });

  test("with the small window open the chip lands there, and no Drawer opens", async () => {
    await onPage();
    await click(byText(view?.container ?? document.body, "button", "Open the Drawer"));
    await until(() => drawer()?.textContent?.includes("which tickets are blocked?"));
    await click(drawer()?.querySelector('[aria-label="Float"]'));
    await until(() => mini());
    await rightClick(at("[data-cell]"));
    await choose("Add to current conversation");
    expect(drawer()).toBeNull();
    expect(chips(mini())).toEqual([ROW]);
    expect(document.activeElement).toBe(mini()?.querySelector("textarea"));
  });

  test("with a drawer or a dialog over the page, the Drawer opens on top of it, not a frame under it", async () => {
    await onPage();
    await click(byText(view?.container ?? document.body, "button", "Open the Drawer"));
    await until(() => drawer()?.textContent?.includes("which tickets are blocked?"));
    await click(drawer()?.querySelector('[aria-label="Float"]'));
    await until(() => mini());
    // A modal layer, as another drawer is one: the overlay stack makes the app's root inert.
    const root = document.createElement("div");
    root.id = "root";
    const other = document.createElement("div");
    document.body.append(root, other);
    const closeOther = pushLayer(other);
    try {
      await rightClick(at("[data-cell]"));
      await choose("Add to current conversation");
      await until(() => drawer()?.querySelector("[data-pointed-chip]"));
      expect(chips(drawer())).toEqual([ROW]);
      expect(mini()).toBeNull();
    } finally {
      closeOther();
      root.remove();
      other.remove();
    }
  });

  test("its card is drawn on the page, so the window around the chip cannot cut it off", async () => {
    await onPage();
    await click(byText(view?.container ?? document.body, "button", "Open the Drawer"));
    await until(() => drawer()?.textContent?.includes("which tickets are blocked?"));
    await click(drawer()?.querySelector('[aria-label="Float"]'));
    await until(() => mini());
    await rightClick(at("[data-cell]"));
    await choose("Add to current conversation");
    await click(mini()?.querySelector("[data-pointed-chip] button"));
    const card = document.querySelector<HTMLElement>("[data-hover-card]");
    expect(card?.textContent).toContain(ROW);
    expect(card?.parentElement).toBe(document.body);
    expect(mini()?.contains(card)).toBe(false);
  });

  test("Start new conversation begins an empty one that holds only the new chip", async () => {
    const { send } = await onPage();
    const create = vi.spyOn(api, "createConversation").mockResolvedValue({
      id: 9,
      title: null,
      created_at: "",
      updated_at: "",
      archived_at: null,
    });
    vi.spyOn(api, "renameConversation").mockResolvedValue({
      id: 9,
      title: ROW,
      created_at: "",
      updated_at: "",
      archived_at: null,
    });
    await pointAndAdd();
    await click(drawer()?.querySelector('[aria-label="Close"]'));
    await settle(300);
    await rightClick(at("[data-cell]"));
    await choose("Start new conversation");
    await until(() => drawer()?.querySelector("textarea"));
    await settle();
    expect(drawer()?.textContent).toContain("Ask Factorial Assistant");
    expect(drawer()?.textContent).not.toContain("which tickets are blocked?");
    expect(chips(drawer())).toEqual([ROW]);
    await press("Enter", drawer()?.querySelector("textarea") ?? document);
    await settle();
    expect(create).toHaveBeenCalledOnce();
    expect(send).toHaveBeenCalledExactlyOnceWith(9, {
      text: "",
      page: "/projects/9041/tickets",
      context: [POINTED],
    });
  });

  test("Start new conversation on one with nothing said yet uses it, and makes no other", async () => {
    stubChatApi({ items: [] });
    const send = vi.spyOn(api, "sendAssistantMessage").mockResolvedValue({ turn_id: 4 });
    const create = vi.spyOn(api, "createConversation");
    vi.spyOn(api, "renameConversation").mockResolvedValue({
      id: 8,
      title: ROW,
      created_at: "",
      updated_at: "",
      archived_at: null,
    });
    const mounted = await mountInAssistant(<Page />, { at: "/projects/9041/tickets" });
    view = mounted.view;
    await click(byText(view.container, "button", "Open the Drawer"));
    await until(() => drawer()?.textContent?.includes("Ask Factorial Assistant"));
    await until(() => vi.mocked(api.getAssistantItems).mock.calls.length > 0);
    await settle();
    await click(drawer()?.querySelector('[aria-label="Close"]'));
    await settle(300);
    await rightClick(at("[data-cell]"));
    await choose("Start new conversation");
    await until(() => drawer()?.querySelector("textarea"));
    await settle();
    await press("Enter", drawer()?.querySelector("textarea") ?? document);
    await settle();
    expect(create).not.toHaveBeenCalled();
    expect(send).toHaveBeenCalledExactlyOnceWith(8, {
      text: "",
      page: "/projects/9041/tickets",
      context: [POINTED],
    });
  });

  test("a sixth thing pointed at is refused, and says so", async () => {
    await onPage();
    for (let count = 0; count < 6; count += 1) {
      await rightClick(at("[data-cell]"));
      await choose("Add to current conversation");
    }
    await until(() => drawer()?.querySelector("textarea"));
    expect(chips(drawer())).toHaveLength(5);
    expect(document.body.textContent).toContain("five things");
  });

  test("the sent message keeps its chip", async () => {
    stubChatApi({
      items: [
        chatItem(1, 1, "user_text", {
          text: "Why is this blocked?",
          page: "/projects/9041/tickets",
          context: [POINTED],
        }),
      ],
    });
    const mounted = await mountInAssistant(<Page />, { at: "/projects/9041/tickets" });
    view = mounted.view;
    await click(byText(view.container, "button", "Open the Drawer"));
    await until(() => drawer()?.textContent?.includes("Why is this blocked?"));
    expect(chips(drawer()?.querySelector('[data-speaker="person"]') ?? null)).toEqual([ROW]);
  });
});

describe("files", () => {
  test("a message's files go to the API by name and bytes, and the sent message names them", async () => {
    const { send } = await onPage();
    await click(byText(view?.container ?? document.body, "button", "Open the Drawer"));
    await until(() => drawer()?.textContent?.includes("which tickets are blocked?"));
    const input = drawer()?.querySelector<HTMLInputElement>('input[type="file"]') ?? null;
    Object.defineProperty(input, "files", {
      configurable: true,
      value: [new File(["timed out"], "worker.log", { type: "text/plain" })],
    });
    await act(async () => {
      input?.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await until(() => drawer()?.querySelector("[data-file-chip]"));
    await type(drawer()?.querySelector("textarea"), "Why did it time out?");
    await press("Enter", drawer()?.querySelector("textarea") ?? document);
    await settle();
    expect(send).toHaveBeenCalledExactlyOnceWith(8, {
      text: "Why did it time out?",
      page: "/projects/9041/tickets",
      attachments: [{ name: "worker.log", data: btoa("timed out") }],
    });
  });

  test("a sent message shows the names of its files", async () => {
    stubChatApi({
      items: [
        chatItem(1, 1, "user_text", {
          text: "",
          page: null,
          attachments: [
            { name: "shot.png", media_type: "image/png", size: 400 },
            { name: "worker.log", media_type: "text/plain", size: 9 },
          ],
        }),
      ],
    });
    const mounted = await mountInAssistant(<Page />, { at: "/projects/9041/tickets" });
    view = mounted.view;
    await click(byText(view.container, "button", "Open the Drawer"));
    await until(() => drawer()?.querySelector('[data-speaker="person"] [data-file-chip]'));
    const said = drawer()?.querySelector('[data-speaker="person"]');
    expect(
      [...(said?.querySelectorAll("[data-file-chip]") ?? [])].map((c) => c.textContent),
    ).toEqual(["shot.png", "worker.log"]);
  });
});

describe("the way back from a chip", () => {
  const card = () => document.querySelector<HTMLElement>('[role="dialog"]:not([aria-modal])');
  const linkIn = (name: string) => byText(card() ?? document.body, "a, button", name);

  test("its card links to the page the thing links to, and following it leaves the Drawer", async () => {
    const mounted = await onPage();
    await pointAndAdd();
    await click(drawer()?.querySelector("footer [data-pointed-chip] button"));
    await until(() => card());
    expect(linkIn("Open the linked page")).not.toBeNull();
    await click(linkIn("Open the linked page"));
    await settle(300);
    expect(mounted.where()).toBe("/tickets/212");
    expect(drawer()).toBeNull();
  });

  test("its card shows where the thing was: the page opens and the thing is ringed", async () => {
    const mounted = await onPage();
    await click(byText(view?.container ?? document.body, "button", "Open the Drawer"));
    await until(() => drawer()?.textContent?.includes("which tickets are blocked?"));
    await click(drawer()?.querySelector('[aria-label="Float"]'));
    await until(() => mini());
    await rightClick(at("[data-cell]"));
    await choose("Add to current conversation");
    expect(at("[data-row]").hasAttribute("data-assist-shown")).toBe(false);
    await click(mini()?.querySelector("footer [data-pointed-chip] button"));
    await until(() => card());
    await click(linkIn("Show where this was"));
    await until(() => at("[data-row]").hasAttribute("data-assist-shown"));
    expect(mounted.where()).toBe("/projects/9041/tickets");
    expect(mini()).not.toBeNull();
    expect(card()).toBeNull();
  });

  test("a thing that names no place, or a place that is not this dashboard's, has no link", async () => {
    stubChatApi({
      items: [
        chatItem(1, 1, "user_text", {
          text: "What is this?",
          context: [
            { label: "A control", text: "words" },
            {
              label: "Elsewhere",
              text: "x",
              page: "//evil.example/x",
              href: "https://evil.example",
            },
          ],
        }),
      ],
    });
    const mounted = await mountInAssistant(<Page />, { at: "/projects/9041/tickets" });
    view = mounted.view;
    await click(byText(view.container, "button", "Open the Drawer"));
    await until(() => drawer()?.querySelector("[data-pointed-chip]"));
    for (const chip of drawer()?.querySelectorAll("[data-pointed-chip] button") ?? []) {
      await click(chip);
      await until(() => card());
      expect(card()?.querySelector("a, button")).toBeNull();
      await press("Escape", document);
    }
  });
});
