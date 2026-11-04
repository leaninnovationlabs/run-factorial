// @vitest-environment happy-dom
/** The Drawer's header: New chat, and the history of every conversation to move between or delete. */

import { afterEach, describe, expect, test, vi } from "vitest";
import type { ChatConversation } from "../../api/types";
import { byText, click, type Mounted, mount, press, type } from "../../testDom";
import { ConversationSwitcher } from "./ConversationSwitcher";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
});

const AN_HOUR_AGO = new Date(Date.now() - 3_600_000).toISOString();

function conversation(id: number, title: string | null): ChatConversation {
  return { id, title, created_at: "", updated_at: AN_HOUR_AGO, archived_at: null };
}

const LIST = [
  conversation(8, "Blocked tickets"),
  conversation(7, null),
  conversation(6, "Costs this week"),
];

async function render({
  conversations = LIST,
  currentId = 8,
  isCreating = false,
}: {
  conversations?: ChatConversation[];
  currentId?: number | null;
  isCreating?: boolean;
} = {}) {
  const handlers = { onSelect: vi.fn(), onNew: vi.fn(), onDelete: vi.fn() };
  view = await mount(
    <ConversationSwitcher
      conversations={conversations}
      currentId={currentId}
      isCreating={isCreating}
      {...handlers}
    />,
  );
  return { root: view.container, ...handlers };
}

const history = () =>
  document.querySelector<HTMLButtonElement>('[aria-label="Conversation history"]');
const popup = () => document.querySelector<HTMLElement>('[role="dialog"]');
const rows = () => [...(popup()?.querySelectorAll<HTMLElement>("li") ?? [])];
const rowTitles = () =>
  rows().map((row) => row.querySelector("[data-title]")?.textContent ?? row.textContent);
const search = () =>
  popup()?.querySelector<HTMLInputElement>('input[aria-label="Search conversations"]') ?? null;

async function openHistory() {
  history()?.focus();
  await click(history());
}

describe("ConversationSwitcher", () => {
  test("New chat is an icon button that asks for a new conversation", async () => {
    const { root, onNew } = await render();
    const newChat = root.querySelector<HTMLButtonElement>('[aria-label="New chat"]');
    expect(newChat?.textContent).toBe("");
    await click(newChat);
    expect(onNew).toHaveBeenCalledTimes(1);
  });

  test("New chat is busy while one is being made, and a second press makes no other", async () => {
    const { root, onNew } = await render({ isCreating: true });
    const newChat = root.querySelector<HTMLButtonElement>('[aria-label="New chat"]');
    expect(newChat?.getAttribute("aria-busy")).toBe("true");
    await click(newChat);
    expect(onNew).not.toHaveBeenCalled();
  });

  test("the history button opens one flat list of every conversation, the open one marked", async () => {
    const many = Array.from({ length: 25 }, (_, index) =>
      conversation(100 - index, `Chat ${index}`),
    );
    await render({ conversations: many, currentId: 99 });
    expect(history()?.getAttribute("aria-haspopup")).toBe("dialog");
    expect(history()?.getAttribute("aria-expanded")).toBe("false");
    await openHistory();
    expect(history()?.getAttribute("aria-expanded")).toBe("true");
    expect(popup()?.getAttribute("aria-label")).toBe("Conversation history");
    expect(rows()).toHaveLength(25);
    expect(popup()?.querySelector('[role="group"]')).toBeNull();
    const current = popup()?.querySelectorAll('[aria-current="true"]');
    expect(current).toHaveLength(1);
    expect(current?.[0]?.querySelector("[data-title]")?.textContent).toBe("Chat 1");
    // A row is a place to go, never a toggle.
    expect(popup()?.querySelector("[aria-pressed]")).toBeNull();
  });

  test("each row shows its title, or New conversation, with its last activity", async () => {
    await render();
    await openHistory();
    expect(rowTitles()).toEqual(["Blocked tickets", "New conversation", "Costs this week"]);
    expect(rows()[0]?.querySelector("time")?.textContent).toBe("1 hour ago");
    expect(rows()[0]?.querySelector("time")?.getAttribute("dateTime")).toBe(AN_HOUR_AGO);
  });

  test("choosing a row opens that conversation and closes the history", async () => {
    const { onSelect } = await render();
    await openHistory();
    await click(
      byText(popup() ?? document.body, "[data-title]", "Costs this week")?.closest("button"),
    );
    expect(onSelect).toHaveBeenCalledExactlyOnceWith(6);
    expect(popup()).toBeNull();
    expect(document.activeElement).toBe(history());
  });

  test("the search filters by title, ignoring case, and says when nothing matches", async () => {
    await render();
    await openHistory();
    expect(document.activeElement).toBe(search());
    await type(search(), "COSTS");
    expect(rowTitles()).toEqual(["Costs this week"]);
    await type(search(), "deploys");
    expect(rows()).toHaveLength(0);
    expect(popup()?.textContent).toContain("No conversations match");
  });

  test("any row's trash asks inline, and Delete deletes that conversation", async () => {
    const { onDelete } = await render();
    await openHistory();
    await click(popup()?.querySelector('[aria-label="Delete Costs this week"]'));
    expect(onDelete).not.toHaveBeenCalled();
    expect(document.querySelectorAll('[role="dialog"]')).toHaveLength(1);
    const asking = rows()[2];
    expect(asking?.textContent).toContain('Delete "Costs this week"?');
    const group = asking?.querySelector('[role="group"]');
    const question = document.getElementById(group?.getAttribute("aria-labelledby") ?? "");
    expect(question?.textContent).toBe('Delete "Costs this week"?');
    expect(document.activeElement).toBe(byText(asking ?? document.body, "button", "Cancel"));
    await click(byText(asking ?? document.body, "button", "Delete"));
    expect(onDelete).toHaveBeenCalledExactlyOnceWith(6);
  });

  test("Cancel or Escape puts the row back, and the history stays open", async () => {
    const { onDelete } = await render();
    await openHistory();
    const trash = () =>
      popup()?.querySelector<HTMLElement>('[aria-label="Delete Blocked tickets"]');
    await click(trash());
    await click(byText(rows()[0] ?? document.body, "button", "Cancel"));
    expect(rows()[0]?.textContent).not.toContain('Delete "');
    expect(document.activeElement).toBe(trash());

    await click(trash());
    await press("Escape", document.activeElement ?? document);
    expect(popup()).not.toBeNull();
    expect(rows()[0]?.textContent).not.toContain('Delete "');
    expect(onDelete).not.toHaveBeenCalled();
  });

  test("deleting the last conversation leaves the focus on New chat, and the history stays shut when one comes back", async () => {
    const only = [conversation(8, "Blocked tickets")];
    const { root, onDelete } = await render({ conversations: only });
    await openHistory();
    await click(popup()?.querySelector('[aria-label="Delete Blocked tickets"]'));
    await click(byText(rows()[0] ?? document.body, "button", "Delete"));
    expect(onDelete).toHaveBeenCalledExactlyOnceWith(8);
    const newChat = root.querySelector('[aria-label="New chat"]');
    expect(document.activeElement).toBe(newChat);

    const props = { currentId: null, onSelect: vi.fn(), onNew: vi.fn(), onDelete };
    await view?.rerender(<ConversationSwitcher conversations={[]} {...props} />);
    expect(history()).toBeNull();
    await view?.rerender(<ConversationSwitcher conversations={LIST} {...props} />);
    expect(history()?.getAttribute("aria-expanded")).toBe("false");
    expect(popup()).toBeNull();
    expect(document.activeElement).toBe(newChat);
  });

  test("a list emptied elsewhere while the history is open shuts it for good", async () => {
    const { onSelect, onNew, onDelete } = await render();
    await openHistory();
    const props = { currentId: null, onSelect, onNew, onDelete };
    await view?.rerender(<ConversationSwitcher conversations={[]} {...props} />);
    await view?.rerender(<ConversationSwitcher conversations={LIST} {...props} />);
    expect(popup()).toBeNull();
    expect(history()?.getAttribute("aria-expanded")).toBe("false");
  });

  test("with no conversations there is no history button", async () => {
    await render({ conversations: [], currentId: null });
    expect(history()).toBeNull();
  });
});
