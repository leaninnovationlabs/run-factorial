// @vitest-environment happy-dom
/**
 * Show me, through the provider and the real assistant page: the chip hands the conversation over
 * to the page it names, and Back to chat hands it back.
 */

import { act } from "react";
import { Link } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { api } from "../../../api/client";
import type { AssistantSettingsOut, ChatItem } from "../../../api/types";
import { byText, click, type Mounted, settle } from "../../../testDom";
import { Button } from "../../ui";
import { useAssistant } from "../AssistantProvider";
import { ANCHOR_WAIT_MS } from "../showMe";
import { chatItem, layOutStreams, mountInAssistant, sseBody } from "../testFakes";
import { asShowMe, ShowMeChip } from "./ShowMeChip";

let view: Mounted | null = null;

const SETTINGS = {
  settings: { enabled: true },
  credentials: [
    {
      backend: "claude_sdk",
      auth_mode: "api_key",
      updated_by: "operator@example.com",
      updated_at: "2026-10-01T09:00:00Z",
    },
  ],
  ready_to_send: true,
  backends: [],
  status: null,
} as unknown as AssistantSettingsOut;

function stubChat(items: ChatItem[]) {
  vi.spyOn(api, "getAssistantSettings").mockResolvedValue(SETTINGS);
  vi.spyOn(api, "listConversations").mockResolvedValue([
    { id: 7, title: "Blocked tickets", created_at: "", updated_at: "", archived_at: null },
  ]);
  vi.spyOn(api, "getAssistantItems").mockResolvedValue({ items, turn: null });
  vi.spyOn(api, "openConversationStream").mockImplementation(async () => sseBody().body);
}

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function Opener() {
  const { open } = useAssistant();
  return (
    <>
      <Button onClick={open}>Open the assistant</Button>
      <Link to="/overview">Leave</Link>
    </>
  );
}

function DrawerOpener() {
  const { openDrawer } = useAssistant();
  return <Button onClick={openDrawer}>Open the Drawer</Button>;
}

/** The assistant's page: `null` while the reader is on another. */
const dialog = () => document.querySelector<HTMLElement>("[data-assistant-chat]");

async function openChat(root: HTMLElement) {
  await click(byText(root, "button", "Open the assistant"));
  for (let tries = 0; tries < 20 && !byText(document.body, "button", "Show me"); tries += 1) {
    await settle();
  }
}

function showMe(card: Record<string, unknown>) {
  return [
    chatItem(1, 1, "user_text", { text: "where do I retry?", page: "/overview" }),
    chatItem(2, 2, "show_me", {
      path: "/tickets/212",
      anchor: null,
      note: null,
      ...card,
    }),
  ];
}

describe("ShowMeChip", () => {
  test("draws no chip for a path that is another site", async () => {
    for (const path of [
      "//evil.example/tickets",
      "/\\evil.example/tickets",
      "https://evil.example",
    ]) {
      view = (
        await mountInAssistant(
          <ShowMeChip payload={{ path, tab: null, anchor: null, note: "Here" }} />,
        )
      ).view;
      expect(byText(view.container, "button", "Show me"), path).toBeNull();
      await view.unmount();
      view = null;
    }
    view = (
      await mountInAssistant(
        <ShowMeChip payload={{ path: "/tickets/212", tab: null, anchor: null, note: "Here" }} />,
      )
    ).view;
    expect(byText(view.container, "button", "Show me")).not.toBeNull();
  });

  test("ShowMeChip leaves the chat for the page, highlights the anchor and offers Back to chat", async () => {
    stubChat(showMe({ anchor: "ticket.actions", note: "Retry is in the ticket's actions" }));
    const mounted = await mountInAssistant(<Opener />, {
      path: "/tickets/:ticketId",
      page: <div data-assist-anchor="ticket.actions">Actions</div>,
    });
    view = mounted.view;
    await openChat(view.container);
    expect(dialog()).not.toBeNull();

    await click(byText(document.body, "button", "Show me"));
    await settle();
    expect(dialog()).toBeNull();
    expect(mounted.where()).toBe("/tickets/212");
    const anchor = document.querySelector('[data-assist-anchor="ticket.actions"]');
    expect(anchor?.hasAttribute("data-assist-shown")).toBe(true);
    expect(anchor?.scrollIntoView).toHaveBeenCalled();
    expect(document.querySelector('[role="note"]')?.textContent).toBe(
      "Retry is in the ticket's actions",
    );

    await click(byText(document.body, "button", "Back to chat"));
    for (let tries = 0; tries < 20 && !dialog(); tries += 1) await settle();
    expect(dialog()).not.toBeNull();
    expect(anchor?.hasAttribute("data-assist-shown")).toBe(false);
    expect(document.querySelector('[role="note"]')).toBeNull();
    expect(api.getAssistantItems).toHaveBeenLastCalledWith(7);
    expect(byText(document.body, "button", "Back to chat")).toBeNull();

    // Back in the chat, the pointing is over: leaving the chat does not bring it back.
    await click(byText(document.body, "a", "Leave"));
    await settle(300);
    expect(dialog()).toBeNull();
    expect(byText(document.body, "button", "Back to chat")).toBeNull();
    expect(anchor?.hasAttribute("data-assist-shown")).toBe(false);
  });

  test("Back to chat opens the conversation where the reader left it, once its items have arrived", async () => {
    const undo = layOutStreams();
    try {
      stubChat(showMe({ anchor: "ticket.actions", note: null }));
      const mounted = await mountInAssistant(<Opener />, {
        path: "/tickets/:ticketId",
        page: <div data-assist-anchor="ticket.actions">Actions</div>,
      });
      view = mounted.view;
      await openChat(view.container);
      const stream = () => document.querySelector<HTMLElement>("[data-item-stream]");
      expect(stream()?.scrollTop).toBe(700);
      const log = stream();
      if (!log) throw new Error("no stream");
      log.scrollTop = 200;
      log.dispatchEvent(new Event("scroll"));

      await click(byText(document.body, "button", "Show me"));
      await settle();
      await click(byText(document.body, "button", "Back to chat"));
      // The page mounts empty and its items are read again: they land after it.
      for (let tries = 0; tries < 20 && !byText(document.body, "button", "Show me"); tries += 1) {
        await settle();
      }
      expect(stream()?.scrollTop).toBe(200);
    } finally {
      undo();
    }
  });

  test("Show me pressed in the Drawer brings Back to chat to the Drawer, not the page", async () => {
    stubChat(showMe({ anchor: "ticket.actions", note: null }));
    const mounted = await mountInAssistant(<DrawerOpener />, {
      at: "/projects/9041",
      path: "/tickets/:ticketId",
      page: <div data-assist-anchor="ticket.actions">Actions</div>,
    });
    view = mounted.view;
    await click(byText(view.container, "button", "Open the Drawer"));
    for (let tries = 0; tries < 20 && !byText(document.body, "button", "Show me"); tries += 1) {
      await settle();
    }
    const drawer = () => document.querySelector('[role="dialog"][aria-modal="true"]');
    expect(drawer()).not.toBeNull();

    await click(byText(document.body, "button", "Show me"));
    await settle();
    expect(drawer()).toBeNull();
    expect(mounted.where()).toBe("/tickets/212");

    await click(byText(document.body, "button", "Back to chat"));
    for (let tries = 0; tries < 20 && !drawer(); tries += 1) await settle();
    expect(drawer()).not.toBeNull();
    // Over the page Show me went to, not the assistant's own page.
    expect(mounted.where()).toBe("/tickets/212");
    expect(dialog()).toBeNull();
  });

  test("Show me on a page without its anchor shows the note without a ring, and never a spinner", async () => {
    stubChat(showMe({ path: "/overview", anchor: "overview.board", note: "The board is here" }));
    const mounted = await mountInAssistant(<Opener />, { path: "/overview", page: <p>empty</p> });
    view = mounted.view;
    await openChat(view.container);
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    await click(byText(document.body, "button", "Show me"));
    expect(document.querySelector('[role="note"]')).toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(ANCHOR_WAIT_MS);
    });
    expect(document.querySelector("[data-assist-shown]")).toBeNull();
    expect(document.querySelector('[role="note"]')?.textContent).toBe("The board is here");
    expect(document.querySelector('[role="status"], [aria-busy="true"]')).toBeNull();
    expect(byText(document.body, "button", "Back to chat")).not.toBeNull();
  });

  test("Show me skips the page that is fading out, and rings the anchor once the new one has it", async () => {
    stubChat(showMe({ anchor: "ticket.actions", note: null }));
    const mounted = await mountInAssistant(<Opener />, {
      path: "/tickets/:ticketId",
      page: <div data-assist-anchor="ticket.actions">Actions</div>,
    });
    view = mounted.view;
    const old = document.createElement("div");
    old.dataset.pagePhase = "out";
    old.innerHTML = '<div data-assist-anchor="ticket.actions">Old</div>';
    // First in the document, as the page fading out is: a search in document order meets it first.
    document.body.prepend(old);
    await openChat(view.container);
    await click(byText(document.body, "button", "Show me"));
    await settle();
    expect(old.querySelector("[data-assist-shown]")).toBeNull();
    expect(view.container.querySelector("[data-assist-shown]")?.textContent).toBe("Actions");
  });
});

describe("a card stored before a project had pages", () => {
  test("opens the page its tab named, on the control that replaced its tab strip", () => {
    expect(asShowMe({ path: "/projects/3", tab: "tickets", anchor: "project.tabs" })).toEqual({
      path: "/projects/3/tickets",
      anchor: "project.nav",
      note: null,
    });
    expect(
      asShowMe({ path: "/projects/3", tab: "configuration", anchor: "process.commit" }),
    ).toEqual({ path: "/projects/3/configuration/process", anchor: "process.commit", note: null });
  });
});
