// @vitest-environment happy-dom
/**
 * The conversation's frames beside the Drawer: the small window that floats over the page, and
 * the bar it collapses to. One conversation and one draft, whichever frame draws them.
 */

import { act } from "react";
import { Link } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { byText, click, type Mounted, press, settle, type } from "../../testDom";
import { BELOW_XL, PHONE } from "../ui/media";
import { AssistantDock, useAssistant } from "./AssistantProvider";
import { FRAMES_KEY } from "./frames";
import { chatItem, chatTurn, mountInAssistant, stubChatApi } from "./testFakes";

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

const drawer = () => document.querySelector<HTMLElement>('[role="dialog"][aria-modal="true"]');
const mini = () => document.querySelector<HTMLElement>("[data-assistant-mini]");
const bar = () => document.querySelector<HTMLElement>("[data-assistant-bar]");
const named = (scope: ParentNode | null, name: string) =>
  scope?.querySelector<HTMLElement>(`[aria-label="${name}"]`) ?? null;
const docked = () => view?.container.querySelector<HTMLElement>("[data-assistant-dock]") ?? null;
/** The window as wide or as narrow as the media queries named: happy-dom's own is 1024 px. */
function windowMatching(...queries: string[]) {
  vi.spyOn(window, "matchMedia").mockImplementation(
    (query: string) =>
      ({
        matches: queries.includes(query),
        addEventListener: () => {},
        removeEventListener: () => {},
      }) as unknown as MediaQueryList,
  );
}
const saved = () => JSON.parse(window.localStorage.getItem(FRAMES_KEY) ?? "{}");

async function until(found: () => unknown) {
  for (let tries = 0; tries < 30 && !found(); tries += 1) await settle();
}

function Opener() {
  const { openDrawer } = useAssistant();
  return (
    <>
      <button type="button" onClick={openDrawer}>
        Open the Drawer
      </button>
      <Link data-to-assistant to="/assistant">
        The assistant's page
      </Link>
      <Link data-to-overview to="/overview">
        Overview
      </Link>
      <AssistantDock />
    </>
  );
}

async function inDrawer(options?: Parameters<typeof stubChatApi>[0]) {
  const chat = stubChatApi({ items: ANSWERED, ...options });
  const mounted = await mountInAssistant(<Opener />, { at: "/projects/9041/tickets" });
  view = mounted.view;
  await click(byText(view.container, "button", "Open the Drawer"));
  await until(() => drawer()?.textContent?.includes("which tickets are blocked?"));
  return { ...mounted, ...chat };
}

async function floating(options?: Parameters<typeof stubChatApi>[0]) {
  const mounted = await inDrawer(options);
  await click(named(drawer(), "Float"));
  await until(() => mini()?.textContent?.includes("which tickets are blocked?"));
  return mounted;
}

async function pointer(type: string, target: EventTarget, at = { x: 0, y: 0 }) {
  await act(async () => {
    target.dispatchEvent(
      new MouseEvent(type, { bubbles: true, cancelable: true, clientX: at.x, clientY: at.y }),
    );
  });
}

describe("the small window", () => {
  test("Float takes the conversation out of the Drawer into a window that is not modal", async () => {
    await floating();
    expect(drawer()).toBeNull();
    expect(mini()?.getAttribute("aria-label")).toBe("Factorial Assistant");
    expect(mini()?.getAttribute("aria-modal")).toBeNull();
    expect(mini()?.textContent).toContain("Two are blocked.");
  });

  test("what is written goes with the conversation into the window and back to the Drawer", async () => {
    await inDrawer();
    await type(drawer()?.querySelector("textarea"), "half a thought");
    await click(named(drawer(), "Float"));
    await until(() => mini()?.querySelector("textarea"));
    expect(mini()?.querySelector("textarea")?.value).toBe("half a thought");
    await type(mini()?.querySelector("textarea"), "half a thought, more");
    await click(named(mini(), "Open in the drawer"));
    await until(() => drawer()?.querySelector("textarea"));
    expect(mini()).toBeNull();
    expect(drawer()?.querySelector("textarea")?.value).toBe("half a thought, more");
  });

  test("Close ends it, and nothing is left on the page", async () => {
    await floating();
    await click(named(mini(), "Close"));
    expect(mini()).toBeNull();
    expect(bar()).toBeNull();
  });

  test("the conversation's text is small unless the reader chose a size, and the choice is kept", async () => {
    await floating();
    const said = () => mini()?.querySelector('[data-speaker="person"] p')?.className ?? "";
    expect(said()).toContain("text-body-sm");
    await click(named(mini(), "Size and text"));
    await click(document.querySelector('input[name="assistant-text"][value="large"]'));
    expect(said()).toContain("text-body-lg");
    expect(said()).not.toContain("text-body-sm");
    expect(saved().text).toBe("large");
  });

  test("its size is one of three, or whatever its corner is dragged to", async () => {
    await floating();
    expect(mini()?.style.width).toBe("384px");
    expect(mini()?.style.height).toBe("480px");
    await click(named(mini(), "Size and text"));
    await click(document.querySelector('input[name="assistant-window"][value="large"]'));
    expect(mini()?.style.width).toBe("448px");
    expect(mini()?.style.height).toBe("576px");

    const corner = mini()?.querySelector('[data-grip="corner"]') as HTMLElement;
    await pointer("pointerdown", corner, { x: 500, y: 300 });
    await pointer("pointermove", window, { x: 450, y: 260 });
    expect(mini()?.style.width).toBe("498px");
    expect(mini()?.style.height).toBe("616px");
    await pointer("pointerup", window, { x: 450, y: 260 });
    expect(saved().mini).toEqual({ width: 498, height: 616 });
  });

  test("a dragged size never leaves the window", async () => {
    await floating();
    const corner = mini()?.querySelector('[data-grip="corner"]') as HTMLElement;
    await pointer("pointerdown", corner, { x: 500, y: 300 });
    await pointer("pointermove", window, { x: -5000, y: -5000 });
    expect(mini()?.style.width).toBe(`${window.innerWidth - 32}px`);
    expect(mini()?.style.height).toBe(`${window.innerHeight - 32}px`);
    await pointer("pointerup", window);
  });

  test("see-through: a press turns it on and off, a held press is a peek, and Escape ends it", async () => {
    await floating();
    const eye = () => named(mini(), "See through") as HTMLElement;
    const through = () => mini()?.hasAttribute("data-see-through");
    await pointer("pointerdown", eye());
    await pointer("pointerup", eye());
    expect(through()).toBe(true);
    expect(eye().getAttribute("aria-pressed")).toBe("true");
    await pointer("pointerdown", eye());
    await pointer("pointerup", eye());
    expect(through()).toBe(false);

    await pointer("pointerdown", eye());
    expect(through()).toBe(true);
    await settle(400);
    await pointer("pointerup", eye());
    expect(through()).toBe(false);

    await pointer("pointerdown", eye());
    await pointer("pointerup", eye());
    expect(through()).toBe(true);
    await press("Escape", document);
    expect(through()).toBe(false);
  });

  test("on a phone the Drawer has no Dock, Float or Minimize: the Drawer is already the window", async () => {
    windowMatching(PHONE, BELOW_XL);
    await inDrawer();
    expect(named(drawer(), "Close")).not.toBeNull();
    for (const name of ["Dock to the side", "Float", "Minimize"]) {
      expect(named(drawer(), name), name).toBeNull();
    }
  });
});

describe("the small window and the pages around it", () => {
  test("Show me from the window goes to the page and keeps the window, with nothing to go back to", async () => {
    const mounted = await floating({
      items: [
        ...ANSWERED,
        chatItem(3, 3, "show_me", { path: "/projects/9041/configuration", note: "It is here." }),
      ],
    });
    await click(byText(mini() ?? document.body, "button", "Show me"));
    await settle();
    expect(mounted.where()).toBe("/projects/9041/configuration");
    expect(mini()?.textContent).toContain("which tickets are blocked?");
    expect(byText(document.body, "button", "Back to chat")).toBeNull();
  });

  test("it is not drawn on the assistant's own page, and is back on the next page", async () => {
    const mounted = await floating();
    await click(view?.container.querySelector("[data-to-assistant]"));
    await until(() => mounted.where() === "/assistant");
    expect(mini()).toBeNull();
    await click(document.querySelector("[data-to-overview]"));
    await until(() => mounted.where() === "/overview");
    await until(() => mini());
    expect(mini()).not.toBeNull();
  });
});

describe("the docked panel", () => {
  async function inDock() {
    windowMatching();
    const mounted = await floating();
    await click(named(mini(), "Dock to the side"));
    await until(() => docked()?.textContent?.includes("which tickets are blocked?"));
    return mounted;
  }

  test("Dock to the side makes the conversation a column of the page, and Float takes it back", async () => {
    await inDock();
    expect(mini()).toBeNull();
    expect(docked()?.getAttribute("aria-label")).toBe("Factorial Assistant");
    await click(named(docked(), "Float"));
    await until(() => mini());
    expect(docked()).toBeNull();
  });

  test("the Drawer goes straight to the docked column, or straight to the window", async () => {
    windowMatching();
    await inDrawer();
    await click(named(drawer(), "Dock to the side"));
    await until(() => docked()?.textContent?.includes("which tickets are blocked?"));
    expect(drawer()).toBeNull();
    expect(mini()).toBeNull();
    await click(named(docked(), "Open in the drawer"));
    await until(() => drawer()?.querySelector("textarea"));
    await click(named(drawer(), "Float"));
    await until(() => mini()?.textContent?.includes("which tickets are blocked?"));
    expect(drawer()).toBeNull();
    expect(docked()).toBeNull();
  });

  test("in a window under 1280 px neither the Drawer nor the window offers Dock, and both still float", async () => {
    windowMatching(BELOW_XL);
    await inDrawer();
    expect(named(drawer(), "Dock to the side")).toBeNull();
    await click(named(drawer(), "Float"));
    await until(() => mini());
    expect(named(mini(), "Dock to the side")).toBeNull();
  });

  test("its width follows its left edge, by pointer or by arrow key, between its limits", async () => {
    await inDock();
    const edge = () => docked()?.querySelector('[role="separator"]') as HTMLElement;
    expect(docked()?.style.width).toBe("400px");
    await pointer("pointerdown", edge(), { x: 600, y: 300 });
    await pointer("pointermove", window, { x: 500, y: 300 });
    expect(docked()?.style.width).toBe("500px");
    await pointer("pointerup", window, { x: 500, y: 300 });
    expect(saved().dock).toBe(500);
    expect(edge().getAttribute("aria-valuenow")).toBe("500");

    await press("ArrowRight", edge());
    expect(docked()?.style.width).toBe("484px");
    await press("ArrowLeft", edge());
    await press("ArrowLeft", edge());
    expect(docked()?.style.width).toBe("516px");

    await pointer("pointerdown", edge(), { x: 600, y: 300 });
    await pointer("pointermove", window, { x: -9000, y: 300 });
    expect(docked()?.style.width).toBe("640px");
    await pointer("pointerup", window, { x: -9000, y: 300 });
    await pointer("pointerdown", edge(), { x: 600, y: 300 });
    await pointer("pointerup", window, { x: 9000, y: 300 });
    expect(docked()?.style.width).toBe("320px");
  });

  test("its header has New chat and the history, a text size, and no see-through", async () => {
    await inDock();
    expect(named(docked(), "New chat")).not.toBeNull();
    expect(named(docked(), "Conversation history")).not.toBeNull();
    expect(named(docked(), "See through")).toBeNull();
    await click(named(docked(), "Text size"));
    expect(document.querySelector('input[name="assistant-text"]')).not.toBeNull();
    expect(document.querySelector('input[name="assistant-window"]')).toBeNull();
    await click(document.querySelector('input[name="assistant-text"][value="default"]'));
    expect(docked()?.querySelector('[data-speaker="person"] p')?.className).toContain("text-body");
    expect(docked()?.querySelector('[data-speaker="person"] p')?.className).not.toContain(
      "text-body-sm",
    );
  });
});

describe("the frames and the page", () => {
  const LINKED = [
    ...ANSWERED,
    chatItem(3, 3, "assistant_text", { text: "See [the ticket](/tickets/212)." }),
  ];

  test("a link in a reply goes to its page and leaves a floating conversation open", async () => {
    const mounted = await floating({ items: LINKED });
    await click(byText(mini() ?? document.body, "a", "the ticket"));
    await until(() => mounted.where() === "/tickets/212");
    expect(mini()?.textContent).toContain("which tickets are blocked?");
  });

  test("a link in a reply leaves a docked conversation open too", async () => {
    windowMatching();
    const mounted = await floating({ items: LINKED });
    await click(named(mini(), "Dock to the side"));
    await until(() => docked());
    await click(byText(docked() ?? document.body, "a", "the ticket"));
    await until(() => mounted.where() === "/tickets/212");
    expect(docked()?.textContent).toContain("which tickets are blocked?");
  });

  const DRAFTED = [
    chatItem(1, 1, "user_text", { text: "which tickets are blocked?" }),
    chatItem(2, 1, "editor_draft", {
      project_id: 9041,
      process: "standard",
      content: "name: standard\n",
      files: [],
      edits: [],
      base_sha: "a".repeat(40),
      would_write: [{ path: ".factorial/processes/process-standard.yaml", origin: "process" }],
    }),
  ];
  const EDITOR = "/projects/9041/configuration/process?draft=2";

  test("a draft opened in the process editor leaves a floating conversation open beside it", async () => {
    const mounted = await floating({ items: DRAFTED });
    await click(byText(mini() ?? document.body, "button", "Open in the process editor"));
    await until(() => mounted.where() === EDITOR);
    expect(mini()?.textContent).toContain("which tickets are blocked?");
  });

  test("a draft opened in the process editor leaves a docked conversation open too", async () => {
    windowMatching();
    const mounted = await inDrawer({ items: DRAFTED });
    await click(named(drawer(), "Dock to the side"));
    await until(() => docked());
    await click(byText(docked() ?? document.body, "button", "Open in the process editor"));
    await until(() => mounted.where() === EDITOR);
    expect(docked()?.textContent).toContain("which tickets are blocked?");
  });

  test("a draft opened from the Drawer closes it, since the Drawer covers the editor", async () => {
    const mounted = await inDrawer({ items: DRAFTED });
    await click(byText(drawer() ?? document.body, "button", "Open in the process editor"));
    await until(() => mounted.where() === EDITOR);
    expect(drawer()).toBeNull();
    expect(mini()).toBeNull();
  });

  test("the window and the bar are drawn inside the app, where a drawer or a dialog opened later makes them inert with the page", async () => {
    await floating();
    expect(view?.container.contains(mini())).toBe(true);
    await click(named(mini(), "Minimize"));
    expect(view?.container.contains(bar())).toBe(true);
  });

  test("a docked conversation floats when the window narrows under 1280 px, and docks again when it widens", async () => {
    const listeners = new Set<() => void>();
    let narrow = false;
    vi.spyOn(window, "matchMedia").mockImplementation(
      (query: string) =>
        ({
          get matches() {
            return query === BELOW_XL && narrow;
          },
          addEventListener: (_: string, heard: () => void) => listeners.add(heard),
          removeEventListener: (_: string, heard: () => void) => listeners.delete(heard),
        }) as unknown as MediaQueryList,
    );
    await floating();
    await click(named(mini(), "Dock to the side"));
    await until(() => docked());
    narrow = true;
    await act(async () => {
      for (const heard of listeners) heard();
    });
    await until(() => mini());
    expect(docked()).toBeNull();
    narrow = false;
    await act(async () => {
      for (const heard of listeners) heard();
    });
    await until(() => docked());
    expect(mini()).toBeNull();
  });
});

describe("the bar", () => {
  test("Minimize is in every frame, and the bar opens the frame it came from", async () => {
    windowMatching();
    await inDrawer();
    await click(named(drawer(), "Minimize"));
    expect(drawer()).toBeNull();
    expect(bar()?.textContent).toContain("Assistant");
    await click(bar());
    await until(() => drawer()?.querySelector("textarea"));
    expect(bar()).toBeNull();

    await click(named(drawer(), "Dock to the side"));
    await until(() => docked());
    await click(named(docked(), "Minimize"));
    expect(docked()).toBeNull();
    await click(bar());
    await until(() => docked());
    expect(docked()?.textContent).toContain("which tickets are blocked?");
  });

  test("Minimize leaves a bar, and pressing the bar opens the window again", async () => {
    await floating();
    await click(named(mini(), "Minimize"));
    expect(mini()).toBeNull();
    expect(bar()?.textContent).toContain("Assistant");
    await click(bar());
    await until(() => mini());
    expect(bar()).toBeNull();
    expect(mini()?.textContent).toContain("which tickets are blocked?");
  });

  test("it shows a reply being written, then that one arrived, until the reader looks", async () => {
    const { stream } = await floating({ items: ANSWERED, turn: chatTurn(4, "running") });
    await click(named(mini(), "Minimize"));
    expect(bar()?.querySelector("[data-working-dots]")).not.toBeNull();
    expect(bar()?.textContent).not.toContain("New reply");
    await act(async () => {
      stream(8)?.turn(chatTurn(4, "done"));
    });
    await until(() => bar()?.textContent?.includes("New reply"));
    expect(bar()?.querySelector("[data-working-dots]")).toBeNull();
    expect(bar()?.textContent).toContain("New reply");

    await click(bar());
    await until(() => mini());
    await click(named(mini(), "Minimize"));
    expect(bar()?.textContent).not.toContain("New reply");
  });
});
