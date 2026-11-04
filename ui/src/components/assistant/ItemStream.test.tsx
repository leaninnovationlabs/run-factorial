// @vitest-environment happy-dom
/** The conversation's items, each drawn by its kind, grouped by who spoke, and the reader kept
 *  at the foot. */

import { act, useState } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { api } from "../../api/client";
import type { ChatItem, ChatTurn } from "../../api/types";
import { click, type Mounted, settle } from "../../testDom";
import { ItemStream } from "./ItemStream";
import { chatItem, chatTurn, layOutStreams, mountInAssistant } from "./testFakes";

let view: Mounted | null = null;

const CARD = '{"card_item_id":9}';

beforeEach(() => {
  vi.spyOn(api, "authMe").mockResolvedValue({ email: "operator@example.com" });
});

afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.restoreAllMocks();
});

const tool = (id: number, name: string, extra: Record<string, unknown> = {}) =>
  chatItem(id, id, "tool", { name, input: {}, status: "done", ok: true, summary: "{}", ...extra });

const ALL: ChatItem[] = [
  chatItem(1, 1, "user_text", { text: "retry 212 and chart the costs" }, { turn_id: 3 }),
  chatItem(2, 9, "assistant_text", { text: "On it." }, { turn_id: 3 }),
  chatItem(3, 3, "tool", {
    name: "factorial_ticket_get",
    input: { ticket: "acme/api#212" },
    status: "done",
    ok: true,
    summary: "{}",
  }),
  chatItem(4, 4, "tool", {
    name: "factorial_chart",
    status: "done",
    ok: true,
    summary: '{"card_item_id":6}',
  }),
  chatItem(5, 5, "proposal", {}),
  chatItem(6, 6, "chart", {
    kind: "bar",
    title: "Cost per ticket",
    unit: "USD",
    x: ["212"],
    series: [{ name: "cost", values: [1] }],
    source: "factorial_costs_get",
  }),
  chatItem(7, 7, "show_me", {
    path: "/tickets/212",
    tab: null,
    anchor: "ticket.actions",
    note: "Here",
  }),
  chatItem(8, 8, "editor_draft", { project_id: 3, process: "standard", would_write: [] }),
  chatItem(
    10,
    10,
    "notice",
    { text: "Stopped at the per-turn limit.", reason: "budget" },
    { turn_id: 3 },
  ),
  chatItem(11, 11, "some_future_kind", { text: "unknown" }),
];

async function render(
  items: ChatItem[],
  turn: ChatTurn | null = null,
  scrollTop: { current: number | null } = { current: null },
) {
  const mounted = await mountInAssistant(
    <ItemStream items={items} turn={turn} onRetry={vi.fn()} scrollTop={scrollTop} />,
  );
  view = mounted.view;
  await settle();
  return mounted.view;
}

const blocks = (root: ParentNode) =>
  [...root.querySelectorAll<HTMLElement>("[data-speaker]")].map((block) => block.dataset.speaker);
/** What the stream's one status region says, and whether the dots are drawn: the two agree. */
function typing(root: ParentNode) {
  const region = root.querySelector('[data-item-stream] [role="status"]');
  expect(region).not.toBeNull();
  const said = region?.textContent;
  const dots = root.querySelector(".assist-typing-dot") !== null;
  expect(dots).toBe(said === "Factorial Assistant is replying");
  return dots ? said : null;
}

describe("ItemStream", () => {
  test("draws each kind of item in the order it was made, and nothing for a card not drawn yet", async () => {
    const root = (await render(ALL)).container;
    const text = root.textContent ?? "";
    const order = [
      "retry 212 and chart the costs",
      "On it.",
      "Used 1 tool",
      "Cost per ticket",
      "Show me",
      "Open in the process editor",
      "Stopped at the per-turn limit.",
    ].map((part) => text.indexOf(part));
    expect(order.every((at) => at >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
    expect(text).not.toContain("Draw a chart");
    expect(text).not.toContain("unknown");
    expect(root.querySelector('[aria-label^="Proposal"]')).toBeNull();
  });

  test("each message is its own row, and everything the assistant did in reply is one block", async () => {
    const root = (
      await render([
        ...ALL,
        chatItem(12, 12, "user_text", { text: "and 213?" }, { turn_id: 4 }),
        chatItem(13, 13, "assistant_text", { text: "Also blocked." }, { turn_id: 4 }),
      ])
    ).container;
    expect(blocks(root)).toEqual(["person", "assistant", "person", "assistant"]);

    const [first, reply] = root.querySelectorAll<HTMLElement>("[data-speaker]");
    if (!first || !reply) throw new Error("no blocks");
    expect(first.querySelector("p")?.textContent).toBe("You said retry 212 and chart the costs");
    expect(first.querySelector("[aria-hidden]")?.textContent).toBe("O");
    expect(reply.querySelectorAll("img[data-nav-mark]")).toHaveLength(1);
    expect(reply.textContent).toContain("Factorial Assistant");
    expect(reply.textContent).toContain("Cost per ticket");
    expect(reply.textContent).toContain("Stopped at the per-turn limit.");
  });

  test("a run of tool calls is one row, and a call that drew a card does not break it", async () => {
    const root = (
      await render([
        chatItem(1, 1, "user_text", { text: "what is blocked" }),
        tool(2, "factorial_tickets_list"),
        tool(3, "factorial_ticket_get"),
        tool(4, "factorial_chart", { summary: '{"card_item_id":9}' }),
        tool(5, "factorial_runs_list", { ok: false, summary: "no runs" }),
        chatItem(6, 6, "assistant_text", { text: "Two are." }),
        tool(7, "factorial_costs_get"),
      ])
    ).container;
    const rows = [...root.querySelectorAll("button[aria-expanded]")].map((row) => row.textContent);
    expect(rows).toEqual(["Used 3 tools (1 failed)", "Used 1 tool"]);

    const [first] = root.querySelectorAll("button[aria-expanded]");
    await click(first);
    expect(root.textContent).toContain("List step runs");
    expect(root.textContent).not.toContain("Draw a chart");
  });

  test("an open run stays open when its first call turns out to have drawn a card", async () => {
    const chart = tool(2, "factorial_chart", { status: "running", summary: undefined });
    const asked = chatItem(1, 1, "user_text", { text: "chart the costs" });
    const costs = tool(3, "factorial_costs_get");
    const mounted = await mountInAssistant(
      <Live first={[asked, chart, costs]} scrollTop={{ current: null }} />,
    );
    view = mounted.view;
    const root = mounted.view.container;
    await click(root.querySelector("button[aria-expanded]"));

    const drew = { ...chart, payload: { ...chart.payload, status: "done", summary: CARD } };
    await act(async () => show([asked, drew, costs]));
    const row = root.querySelector("button[aria-expanded]");
    expect(row?.textContent).toBe("Used 1 tool");
    expect(row?.getAttribute("aria-expanded")).toBe("true");
  });

  test("shows the assistant typing while its turn is live and no text is arriving", async () => {
    const asked = chatItem(1, 1, "user_text", { text: "what is blocked" }, { turn_id: 3 });
    const live = chatTurn(3, "running");

    let root = (await render([asked], chatTurn(3, "queued"))).container;
    expect(typing(root)).not.toBeNull();
    // The dots are drawn only; the words are the region's, which was there before them.
    expect(root.querySelector(".assist-typing-dot")?.closest("[aria-hidden]")).not.toBeNull();
    // The reply has not begun, so the dots start the assistant's block, mark and all.
    expect(blocks(root)).toEqual(["person", "assistant"]);
    expect(root.querySelector('[data-speaker="assistant"] img[data-nav-mark]')).not.toBeNull();
    await view?.unmount();

    root = (await render([asked, tool(2, "factorial_tickets_list", { status: "running" })], live))
      .container;
    expect(typing(root)).not.toBeNull();
    expect(blocks(root)).toEqual(["person", "assistant"]);
    await view?.unmount();

    const writing = chatItem(3, 3, "assistant_text", { text: "Two" }, { final: false });
    root = (await render([asked, writing], live)).container;
    expect(typing(root)).toBeNull();
    await view?.unmount();

    root = (await render([asked, { ...writing, final: true }], live)).container;
    expect(typing(root)).not.toBeNull();
    await view?.unmount();

    root = (await render([asked, { ...writing, final: true }], chatTurn(3, "done"))).container;
    expect(typing(root)).toBeNull();
  });

  test("keeps the reader at the foot as items arrive, unless they have scrolled up", async () => {
    const scrollTop = { current: null as number | null };
    const mounted = await mountInAssistant(<Live first={ALL.slice(0, 2)} scrollTop={scrollTop} />);
    view = mounted.view;
    const log = mounted.view.container.querySelector<HTMLElement>("[data-item-stream]");
    if (!log) throw new Error("no stream");
    Object.defineProperty(log, "scrollHeight", { configurable: true, value: 1000 });
    Object.defineProperty(log, "clientHeight", { configurable: true, value: 300 });
    log.scrollTop = 700;
    log.dispatchEvent(new Event("scroll"));
    await act(async () => show(ALL.slice(0, 3)));
    expect(log.scrollTop).toBe(1000);

    log.scrollTop = 100;
    log.dispatchEvent(new Event("scroll"));
    expect(scrollTop.current).toBe(100);
    await act(async () => show(ALL.slice(0, 4)));
    expect(log.scrollTop).toBe(100);
  });

  test("opens where the reader left it, once the items it held have arrived", async () => {
    const undo = layOutStreams();
    try {
      const scrollTop = { current: 250 as number | null };
      const mounted = await mountInAssistant(<Live first={[]} scrollTop={scrollTop} />);
      view = mounted.view;
      const log = mounted.view.container.querySelector<HTMLElement>("[data-item-stream]");
      // Empty, the box has nowhere to scroll to: the place waits for the items.
      expect(log?.scrollTop).toBe(0);
      await act(async () => show(ALL));
      expect(log?.scrollTop).toBe(250);
      expect(scrollTop.current).toBe(250);
    } finally {
      undo();
    }
  });
});

let show: (items: ChatItem[]) => void = () => {};

/** The stream with items a test can add to, as the stream hook would. */
function Live({ first, scrollTop }: { first: ChatItem[]; scrollTop: { current: number | null } }) {
  const [items, setItems] = useState(first);
  show = setItems;
  return <ItemStream items={items} turn={null} onRetry={() => {}} scrollTop={scrollTop} />;
}
