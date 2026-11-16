// @vitest-environment happy-dom
/**
 * The assistant's page, opened from another page of the dashboard, against a mocked API: the
 * conversation it opens on, the composer and its turn, the page a message is about, and what it
 * says when the assistant is not set up.
 */

import type { QueryClient } from "@tanstack/react-query";
import { act } from "react";
import { Link } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { ApiError, api } from "../../api/client";
import { keys } from "../../api/keys";
import { MEMBER_PERMISSIONS } from "../../api/organization.fixture";
import type { AssistantSettingsOut, ChatConversation, ChatItem, ChatTurn } from "../../api/types";
import { AssistantPage } from "../../pages/AssistantPage";
import { byText, click, type Mounted, press, settle, type } from "../../testDom";
import { ASSISTANT_PATH, useAssistant } from "./AssistantProvider";
import { chatItem, chatTurn, mountInAssistant, type SseBody, sseBody } from "./testFakes";

let view: Mounted | null = null;

function stored(updatedAt = "2026-10-01T09:00:00Z") {
  return {
    backend: "claude_sdk",
    auth_mode: "api_key",
    updated_by: "operator@example.com",
    updated_at: updatedAt,
  };
}

function settings(enabled = true, credential = true): AssistantSettingsOut {
  return {
    settings: { enabled },
    credentials: credential ? [stored()] : [],
    ready_to_send: enabled && credential,
    backends: [],
    status: null,
  } as unknown as AssistantSettingsOut;
}

function conversation(id: number, title: string | null): ChatConversation {
  return { id, title, created_at: "", updated_at: "2026-10-12T09:00:00Z", archived_at: null };
}

const CONVERSATIONS = [conversation(8, "Blocked tickets"), conversation(7, "Costs")];

/** The API the Drawer reads, and the stream of each conversation it opens, by id. */
function stubApi({
  configured = settings(),
  conversations = CONVERSATIONS,
  items = [],
  turn = null,
}: {
  configured?: AssistantSettingsOut;
  conversations?: ChatConversation[];
  items?: ChatItem[];
  turn?: ChatTurn | null;
} = {}) {
  const streams = new Map<number, SseBody>();
  vi.spyOn(api, "getAssistantSettings").mockResolvedValue(configured);
  // Read from the settings stub, so a test that changes the settings changes this too.
  vi.spyOn(api, "getAssistantReady").mockImplementation(async () => {
    const current = await api.getAssistantSettings();
    return {
      enabled: current.settings.enabled,
      credential_set: current.credentials.length > 0,
      credential_updated_at: current.credentials[0]?.updated_at ?? null,
      ready_to_send: current.ready_to_send,
    };
  });
  vi.spyOn(api, "listConversations").mockResolvedValue(conversations);
  vi.spyOn(api, "getAssistantItems").mockResolvedValue({ items, turn });
  vi.spyOn(api, "openConversationStream").mockImplementation(async (id) => {
    const body = sseBody();
    streams.set(id, body);
    return body.body;
  });
  return { stream: (id: number) => streams.get(id) };
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

/** The conversation's page: `null` once the reader has left it. */
const dialog = () => document.querySelector<HTMLElement>("[data-assistant-chat]");
const newChatButton = () => dialog()?.querySelector<HTMLElement>('[aria-label="New chat"]');
const composer = () => dialog()?.querySelector<HTMLTextAreaElement>("textarea") ?? null;
const stop = () => dialog()?.querySelector<HTMLElement>('[aria-label="Stop"]') ?? null;
const historyButton = () => dialog()?.querySelector('[aria-label="Conversation history"]');
const listed = () =>
  [
    ...document.querySelectorAll('[aria-label="Conversation history"][role="dialog"] [data-title]'),
  ].map((title) => title.textContent);

async function until(found: () => unknown) {
  for (let tries = 0; tries < 30 && !found(); tries += 1) await settle();
}

/** Until the page has read its conversations and opened the latest of them. The composer is drawn
 *  before the list answers, so it is no sign of that: a message sent sooner starts a new
 *  conversation, and how soon the list lands depends on how busy the machine is. */
async function opened(client: QueryClient) {
  await until(() => {
    const listed = client.getQueryData<ChatConversation[]>(keys.conversations);
    const latest = listed?.[0];
    return (
      listed !== undefined &&
      (latest === undefined ||
        vi.mocked(api.getAssistantItems).mock.calls.some(([id]) => id === latest.id))
    );
  });
}

/** On every page: the way into the assistant, as the side navigation's item, and a way out. */
function Ways() {
  const { open } = useAssistant();
  return (
    <>
      <button type="button" onClick={open}>
        Open assistant
      </button>
      <Link to="/overview">Leave</Link>
    </>
  );
}

/** Open the assistant from the page at `at`, which is the page a message is then about. */
async function ask(at = "/overview", permissions?: string[]) {
  const mounted = await mountInAssistant(<Ways />, {
    at,
    path: ASSISTANT_PATH,
    page: <AssistantPage />,
    permissions,
  });
  view = mounted.view;
  await click(byText(view.container, "button", "Open assistant"));
  await until(() => dialog()?.querySelector("textarea, section"));
  await opened(mounted.client);
  await settle();
  return mounted;
}

const ANSWERED = [
  chatItem(
    1,
    1,
    "user_text",
    { text: "which tickets are blocked?", page: "/overview" },
    {
      turn_id: 3,
    },
  ),
  chatItem(2, 2, "assistant_text", { text: "Two are **blocked**." }, { turn_id: 3 }),
];

describe("AssistantPage", () => {
  test("opens on the latest conversation, as a page rather than a dialog", async () => {
    stubApi({ items: ANSWERED });
    const { where } = await ask();
    expect(where()).toBe(ASSISTANT_PATH);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    const title = dialog()?.querySelector("h1");
    expect(title?.textContent).toBe("Factorial Assistant");
    expect(title?.querySelector("img")?.getAttribute("alt")).toBe("");
    expect(api.getAssistantItems).toHaveBeenCalledWith(8);
    expect(dialog()?.textContent).toContain("which tickets are blocked?");
    expect(dialog()?.querySelector("strong")?.textContent).toBe("blocked");
  });

  test("when not set up, the page says so and links to the assistant settings", async () => {
    stubApi({ configured: settings(true, false), items: ANSWERED });
    const { where } = await ask();
    expect(dialog()?.textContent).toContain("Set up the assistant");
    expect(composer()).toBeNull();
    // What was said before stays readable.
    expect(dialog()?.textContent).toContain("which tickets are blocked?");
    await click(byText(dialog() ?? document.body, "a", "Open the assistant settings"));
    expect(where()).toBe("/settings");
    expect(dialog()).toBeNull();
  });

  test("when not set up, a reader who cannot change the settings is told to ask an admin", async () => {
    stubApi({ configured: settings(true, false), items: ANSWERED });
    await ask("/overview", MEMBER_PERMISSIONS);
    expect(dialog()?.textContent).toContain("Set up the assistant");
    expect(dialog()?.textContent).toContain("Ask an admin to set it up.");
    expect(byText(dialog() ?? document.body, "a", "Open the assistant settings")).toBeFalsy();
  });

  test("on Bedrock through the host's role, with no credential stored, the composer is offered", async () => {
    stubApi({ configured: { ...settings(true, false), ready_to_send: true }, items: ANSWERED });
    await ask();
    expect(dialog()?.textContent).not.toContain("Set up the assistant");
    expect(composer()).not.toBeNull();
  });

  test("the composer is disabled while a turn is queued or running, and Stop cancels it", async () => {
    const server = stubApi({ items: ANSWERED, turn: chatTurn(3, "done") });
    const cancel = vi.spyOn(api, "cancelAssistantTurn").mockResolvedValue(undefined);
    await ask();
    expect(composer()?.disabled).toBe(false);

    for (const status of ["queued", "running"] as const) {
      server.stream(8)?.turn(chatTurn(4, status));
      await settle();
      expect(composer()?.disabled).toBe(true);
    }
    await click(stop());
    expect(cancel).toHaveBeenCalledExactlyOnceWith(4);

    server.stream(8)?.turn(chatTurn(4, "cancelled"));
    await settle();
    expect(composer()?.disabled).toBe(false);
    expect(stop()).toBeNull();
  });

  test("a refused Stop is said while its turn runs, and not after the turn has ended", async () => {
    const server = stubApi({ items: ANSWERED, turn: chatTurn(4, "running") });
    vi.spyOn(api, "cancelAssistantTurn").mockRejectedValue(
      new ApiError(0, "Can't reach Factorial right now."),
    );
    await ask();
    await click(stop());
    await settle();
    expect(dialog()?.querySelector('[role="alert"]')?.textContent).toContain(
      "Can't reach Factorial right now.",
    );

    server.stream(8)?.turn(chatTurn(4, "done"));
    await settle();
    expect(dialog()?.querySelector('[role="alert"]')).toBeNull();
  });

  test("a 409 turn_live from a second tab leaves the composer disabled and shows the running turn", async () => {
    const server = stubApi({ items: ANSWERED, turn: chatTurn(3, "done") });
    vi.spyOn(api, "sendAssistantMessage").mockRejectedValue(
      new ApiError(409, "a turn is already queued or running", "turn_live"),
    );
    await ask();
    await type(composer(), "and the costs?");
    await press("Enter", composer() ?? document);
    await settle();
    expect(composer()?.disabled).toBe(true);
    expect(composer()?.value).toBe("and the costs?");

    // The other tab's turn reaches this one through the stream.
    server.stream(8)?.turn(chatTurn(4, "running"));
    await settle();
    expect(stop()).not.toBeNull();
    expect(composer()?.disabled).toBe(true);

    server.stream(8)?.turn(chatTurn(4, "done"));
    await settle();
    expect(composer()?.disabled).toBe(false);
  });

  test("New chat creates a conversation and focuses the composer", async () => {
    stubApi({ items: ANSWERED });
    const create = vi.spyOn(api, "createConversation").mockResolvedValue(conversation(9, null));
    await ask();
    const newChat = newChatButton();
    // A press focuses the button it lands on; happy-dom's click does not.
    newChat?.focus();
    await click(newChat);
    await settle();
    expect(create).toHaveBeenCalledTimes(1);
    expect(api.getAssistantItems).toHaveBeenLastCalledWith(9);
    expect(document.activeElement).toBe(composer());
  });

  test("deleting another conversation from the history keeps the open one", async () => {
    stubApi({ items: ANSWERED, conversations: [...CONVERSATIONS, conversation(6, "Deploys")] });
    const remove = vi.spyOn(api, "deleteConversation").mockResolvedValue(undefined);
    await ask();
    const history = () => dialog()?.querySelector('[aria-label="Conversation history"]');
    await click(history());
    await click(byText(document.body, "[data-title]", "Deploys")?.closest("button"));
    await settle();
    expect(api.getAssistantItems).toHaveBeenLastCalledWith(6);
    await click(history());
    await click(document.querySelector('[aria-label="Delete Blocked tickets"]'));
    await click(byText(document.body, "button", "Delete"));
    await settle();
    expect(remove).toHaveBeenCalledExactlyOnceWith(8);
    expect(api.getAssistantItems).toHaveBeenLastCalledWith(6);
  });

  test("deleting the open conversation opens the next one", async () => {
    stubApi({ items: ANSWERED });
    vi.spyOn(api, "deleteConversation").mockResolvedValue(undefined);
    await ask();
    await click(dialog()?.querySelector('[aria-label="Conversation history"]'));
    await click(document.querySelector('[aria-label="Delete Blocked tickets"]'));
    await click(byText(document.body, "button", "Delete"));
    await settle();
    expect(api.getAssistantItems).toHaveBeenLastCalledWith(7);
  });

  test("a deleted conversation leaves the history at once, and comes back if the delete fails", async () => {
    stubApi({ items: ANSWERED });
    let refuse: (error: Error) => void = () => {};
    vi.spyOn(api, "deleteConversation").mockReturnValue(
      new Promise((_, reject) => {
        refuse = reject;
      }),
    );
    await ask();
    await click(historyButton());
    await click(document.querySelector('[aria-label="Delete Costs"]'));
    await click(byText(document.body, "button", "Delete"));
    expect(listed()).toEqual(["Blocked tickets"]);
    expect(api.getAssistantItems).toHaveBeenLastCalledWith(8);

    await act(async () => refuse(new ApiError(500, "the delete failed")));
    await settle();
    expect(listed()).toEqual(["Blocked tickets", "Costs"]);
    expect(dialog()?.textContent).toContain("the delete failed");
  });

  test("deleting the open conversation twice in a row, before either answers, never lands on one being deleted", async () => {
    stubApi({ items: ANSWERED, conversations: [...CONVERSATIONS, conversation(6, "Deploys")] });
    const remove = vi.spyOn(api, "deleteConversation").mockReturnValue(new Promise(() => {}));
    await ask();
    await click(historyButton());
    await click(document.querySelector('[aria-label="Delete Blocked tickets"]'));
    await click(byText(document.body, "button", "Delete"));
    await settle();
    expect(api.getAssistantItems).toHaveBeenLastCalledWith(7);
    await click(document.querySelector('[aria-label="Delete Costs"]'));
    await click(byText(document.body, "button", "Delete"));
    await settle();
    expect(remove).toHaveBeenCalledTimes(2);
    expect(api.getAssistantItems).toHaveBeenLastCalledWith(6);
  });

  test("the page the reader came from is sent as page context, not the assistant's own", async () => {
    stubApi({ items: [] });
    const send = vi.spyOn(api, "sendAssistantMessage").mockResolvedValue({ turn_id: 4 });
    vi.spyOn(api, "renameConversation").mockResolvedValue(conversation(8, "x"));
    await ask("/tickets/212");
    await type(composer(), "why is this one blocked?");
    await press("Enter", composer() ?? document, { shiftKey: true });
    expect(send).not.toHaveBeenCalled();
    await press("Enter", composer() ?? document);
    await settle();
    expect(send).toHaveBeenCalledExactlyOnceWith(8, {
      text: "why is this one blocked?",
      page: "/tickets/212",
    });
    expect(composer()?.value).toBe("");
  });

  test("opened straight onto the assistant, a message names no page it was not sent from", async () => {
    stubApi({ items: [] });
    const send = vi.spyOn(api, "sendAssistantMessage").mockResolvedValue({ turn_id: 4 });
    vi.spyOn(api, "renameConversation").mockResolvedValue(conversation(8, "x"));
    const mounted = await mountInAssistant(<Ways />, {
      at: ASSISTANT_PATH,
      path: ASSISTANT_PATH,
      page: <AssistantPage />,
    });
    view = mounted.view;
    await opened(mounted.client);
    expect(api.getAssistantItems).toHaveBeenCalledWith(8);
    await type(composer(), "what is blocked?");
    await press("Enter", composer() ?? document);
    await settle();
    expect(send).toHaveBeenCalledExactlyOnceWith(8, { text: "what is blocked?", page: null });
  });

  test("a turn still queued after 10 seconds says the assistant hasn't started on it", async () => {
    const server = stubApi({ items: ANSWERED, turn: chatTurn(3, "done") });
    await ask();
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    server.stream(8)?.turn(chatTurn(4, "queued"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(9_000);
    });
    expect(dialog()?.textContent).not.toContain(
      "The assistant hasn't started on this yet. If nothing happens soon, press Stop and try again.",
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(dialog()?.textContent).toContain(
      "The assistant hasn't started on this yet. If nothing happens soon, press Stop and try again.",
    );
  });

  test("a conversation deleted elsewhere says so, and New chat still works", async () => {
    stubApi({ items: ANSWERED });
    vi.spyOn(api, "getAssistantItems").mockImplementation(async (id) => {
      if (id === 8) throw new ApiError(404, "no such conversation");
      return { items: [], turn: null };
    });
    vi.spyOn(api, "createConversation").mockResolvedValue(conversation(9, null));
    await ask();
    expect(dialog()?.textContent).toContain("Can't open this conversation");
    await click(newChatButton());
    await settle();
    expect(dialog()?.textContent).not.toContain("Can't open this conversation");
    expect(dialog()?.textContent).toContain("Ask Factorial Assistant");
  });

  test("a send never renames a conversation while the list that says its title is unread", async () => {
    stubApi({ items: ANSWERED });
    vi.spyOn(api, "sendAssistantMessage").mockResolvedValue({ turn_id: 4 });
    const rename = vi.spyOn(api, "renameConversation").mockResolvedValue(conversation(8, "x"));
    const { client } = await ask();
    await click(byText(document.body, "a", "Leave"));
    await settle(300);
    // The list is read afresh on the next visit, and has not answered yet.
    client.removeQueries({ queryKey: keys.conversations });
    vi.spyOn(api, "listConversations").mockReturnValue(new Promise(() => {}));
    await click(byText(document.body, "button", "Open assistant"));
    await until(() => composer());
    await type(composer(), "and the costs?");
    await press("Enter", composer() ?? document);
    await settle();
    expect(api.sendAssistantMessage).toHaveBeenCalledWith(8, expect.anything());
    // Taken, though the list has still not answered: a send waits on no read of the list.
    expect(composer()?.value).toBe("");
    expect(rename).not.toHaveBeenCalled();
  });

  test("a not-set-up refusal holds until the settings change, then the composer is back", async () => {
    stubApi({ items: ANSWERED });
    vi.spyOn(api, "sendAssistantMessage").mockRejectedValue(
      new ApiError(409, "Set up the assistant in Assistant settings.", "not_configured"),
    );
    const { client } = await ask();
    await type(composer(), "hello");
    await press("Enter", composer() ?? document);
    await settle();
    expect(dialog()?.textContent).toContain("Set up the assistant in Assistant settings.");
    expect(composer()).toBeNull();

    // Fixed in Assistant settings, which saves and refreshes the settings every view reads.
    vi.mocked(api.getAssistantSettings).mockResolvedValue({
      ...settings(),
      credentials: [stored("2026-10-12T10:00:00Z")],
      ready_to_send: true,
    });
    await act(async () => {
      await client.invalidateQueries({ queryKey: keys.assistantSettings });
    });
    await settle();
    expect(composer()).not.toBeNull();
    expect(dialog()?.querySelector('[role="alert"]')).toBeNull();
  });

  test("a chat started with New chat takes its first message as its title, before the list knows it", async () => {
    stubApi({ items: [] });
    vi.spyOn(api, "createConversation").mockResolvedValue(conversation(9, null));
    vi.spyOn(api, "sendAssistantMessage").mockResolvedValue({ turn_id: 4 });
    const rename = vi.spyOn(api, "renameConversation").mockResolvedValue(conversation(9, "x"));
    await ask();
    await click(newChatButton());
    await settle();
    // The list read again after the create still answers without the new chat.
    await type(composer(), "Why is #212 blocked?");
    await press("Enter", composer() ?? document);
    await settle();
    expect(api.sendAssistantMessage).toHaveBeenCalledWith(9, expect.anything());
    expect(rename).toHaveBeenCalledExactlyOnceWith(9, "Why is #212 blocked?");
  });

  test("a first message names an untitled conversation, and a send with none creates one", async () => {
    stubApi({ conversations: [], items: [] });
    const create = vi.spyOn(api, "createConversation").mockResolvedValue(conversation(9, null));
    const send = vi.spyOn(api, "sendAssistantMessage").mockResolvedValue({ turn_id: 4 });
    const rename = vi.spyOn(api, "renameConversation").mockResolvedValue(conversation(9, "x"));
    await ask();
    await type(composer(), "Which tickets are blocked?\nAnd why?");
    await press("Enter", composer() ?? document);
    await settle();
    expect(create).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledExactlyOnceWith(9, {
      text: "Which tickets are blocked?\nAnd why?",
      page: "/overview",
    });
    expect(rename).toHaveBeenCalledExactlyOnceWith(9, "Which tickets are blocked?");
  });
});

/** A project's navigation's way into the assistant: the Drawer over the page. */
function DrawerOpener() {
  const { openDrawer } = useAssistant();
  return (
    <button type="button" onClick={openDrawer}>
      Open the Drawer
    </button>
  );
}

const drawer = () => document.querySelector<HTMLElement>('[role="dialog"][aria-modal="true"]');

describe("the assistant's Drawer, inside a project", () => {
  test("opens over the project's page, asks about that page, and closes back to it", async () => {
    stubApi({ items: ANSWERED });
    const send = vi.spyOn(api, "sendAssistantMessage").mockResolvedValue({ turn_id: 4 });
    const mounted = await mountInAssistant(<DrawerOpener />, { at: "/projects/9041/tickets" });
    view = mounted.view;
    await click(byText(view.container, "button", "Open the Drawer"));
    await until(() => drawer()?.querySelector("textarea"));
    await opened(mounted.client);
    await settle();

    expect(mounted.where()).toBe("/projects/9041/tickets");
    expect(drawer()?.querySelector("h2")?.textContent).toBe("Factorial Assistant");
    expect(drawer()?.textContent).toContain("which tickets are blocked?");
    const box = drawer()?.querySelector("textarea") ?? null;
    await type(box, "which of these cost the most?");
    await press("Enter", box ?? document);
    await settle();
    expect(send).toHaveBeenCalledExactlyOnceWith(8, {
      text: "which of these cost the most?",
      page: "/projects/9041/tickets",
    });

    await click(drawer()?.querySelector('[aria-label="Close"]'));
    await settle(300);
    expect(drawer()).toBeNull();
    expect(mounted.where()).toBe("/projects/9041/tickets");
  });

  test("a link in the Drawer closes it on the way to the page", async () => {
    stubApi({ configured: settings(true, false), items: ANSWERED });
    const mounted = await mountInAssistant(<DrawerOpener />, { at: "/projects/9041" });
    view = mounted.view;
    await click(byText(view.container, "button", "Open the Drawer"));
    await until(() => drawer()?.textContent?.includes("Set up the assistant"));
    await click(byText(drawer() ?? document.body, "a", "Open the assistant settings"));
    await settle(300);
    expect(mounted.where()).toBe("/settings");
    expect(drawer()).toBeNull();
  });
});
