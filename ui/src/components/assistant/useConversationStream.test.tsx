// @vitest-environment happy-dom
/** The conversation stream, fed one server-sent event at a time. */

import { act } from "react";
import { afterEach, describe, expect, test, vi } from "vitest";
import { ApiError, api } from "../../api/client";
import { type Mounted, mount, settle } from "../../testDom";
import { chatItem, chatTurn, type SseBody, sseBody } from "./testFakes";
import { useConversationStream } from "./useConversationStream";

let view: Mounted | null = null;
let latest: ReturnType<typeof useConversationStream> | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
  latest = null;
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function Probe({ id }: { id: number | null }) {
  latest = useConversationStream(id);
  return null;
}

function current() {
  if (!latest) throw new Error("the probe has not rendered");
  return latest;
}

/** Each connection gets the next body; past the last, a body that never sends. */
function streams(...bodies: SseBody[]) {
  const open = vi.spyOn(api, "openConversationStream");
  for (const body of bodies) open.mockResolvedValueOnce(body.body);
  open.mockImplementation(async () => sseBody().body);
  return open;
}

describe("useConversationStream", () => {
  test("items keep the order they were made in, and a later payload replaces an earlier one", async () => {
    vi.spyOn(api, "getAssistantItems").mockResolvedValue({
      items: [chatItem(1, 1, "user_text", { text: "which tickets are blocked?" })],
      turn: chatTurn(9, "queued"),
    });
    const stream = sseBody();
    streams(stream);
    view = await mount(<Probe id={7} />);
    await settle();

    stream.item(chatItem(2, 2, "assistant_text", { text: "Two" }, { final: false }));
    stream.item(chatItem(3, 3, "tool", { name: "factorial_tickets_list", status: "running" }));
    stream.item(chatItem(2, 4, "assistant_text", { text: "Two are blocked." }));
    await settle();
    expect(current().items.map((item) => [item.id, item.rev])).toEqual([
      [1, 1],
      [2, 4],
      [3, 3],
    ]);
    expect(current().items[1]?.payload.text).toBe("Two are blocked.");

    // A copy older than the one held never replaces it.
    stream.item(chatItem(2, 2, "assistant_text", { text: "Two" }, { final: false }));
    await settle();
    expect(current().items[1]?.payload.text).toBe("Two are blocked.");
    expect(current().items).toHaveLength(3);
  });

  test("a reconnect sends Last-Event-ID of the highest rev seen", async () => {
    vi.spyOn(api, "getAssistantItems").mockResolvedValue({
      items: [chatItem(1, 3, "user_text", { text: "hello" })],
      turn: null,
    });
    const first = sseBody();
    const second = sseBody();
    const open = streams(first, second);
    view = await mount(<Probe id={7} />);
    await settle();
    expect(open).toHaveBeenLastCalledWith(7, 3, expect.any(AbortSignal));
    expect(current().connected).toBe(true);

    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    first.item(chatItem(2, 7, "assistant_text", { text: "Hi." }));
    first.item(chatItem(1, 5, "user_text", { text: "hello" }));
    first.end();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(current().connected).toBe(false);
    expect(open).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(open).toHaveBeenCalledTimes(2);
    expect(open).toHaveBeenLastCalledWith(7, 7, expect.any(AbortSignal));
    expect(current().connected).toBe(true);

    // A connection that delivered something starts the wait from 1 s again.
    second.send(": ping\n\n");
    second.end();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(open).toHaveBeenCalledTimes(3);
  });

  test("a stream that cannot connect is tried again after 1 s, doubling to 15 s", async () => {
    vi.spyOn(api, "getAssistantItems").mockResolvedValue({ items: [], turn: null });
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    const tried: number[] = [];
    vi.spyOn(api, "openConversationStream").mockImplementation(async () => {
      tried.push(Date.now());
      throw new ApiError(0, "offline");
    });
    view = await mount(<Probe id={7} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    const waits = tried.slice(1).map((at, index) => at - (tried[index] ?? 0));
    expect(waits.slice(0, 6)).toEqual([1000, 2000, 4000, 8000, 15_000, 15_000]);
  });

  test("a stream that opens and ends without a word is tried again as if it had failed", async () => {
    vi.spyOn(api, "getAssistantItems").mockResolvedValue({ items: [], turn: null });
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    const tried: number[] = [];
    vi.spyOn(api, "openConversationStream").mockImplementation(async () => {
      tried.push(Date.now());
      const body = sseBody();
      body.end();
      return body.body;
    });
    view = await mount(<Probe id={7} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    const waits = tried.slice(1).map((at, index) => at - (tried[index] ?? 0));
    expect(waits.slice(0, 3)).toEqual([1000, 2000, 4000]);
  });

  test("a conversation the API will not show this reader (404, 401, 403) is not asked for again", async () => {
    for (const status of [404, 401, 403]) {
      vi.spyOn(api, "getAssistantItems").mockResolvedValue({ items: [], turn: null });
      const open = vi
        .spyOn(api, "openConversationStream")
        .mockRejectedValue(new ApiError(status, "no such conversation"));
      vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
      view = await mount(<Probe id={7} />);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000);
      });
      expect(open, String(status)).toHaveBeenCalledTimes(1);
      expect(current()).toMatchObject({ connected: false, gone: true });
      await view.unmount();
      view = null;
      vi.restoreAllMocks();
      vi.useRealTimers();
    }

    // A first read that is refused opens no stream at all.
    vi.spyOn(api, "getAssistantItems").mockRejectedValue(new ApiError(404, "no such conversation"));
    const open = vi.spyOn(api, "openConversationStream");
    view = await mount(<Probe id={7} />);
    await settle();
    expect(open).not.toHaveBeenCalled();
    expect(current().gone).toBe(true);
  });

  test("a stream opened again and again leaves no listener behind on its stop signal", async () => {
    vi.spyOn(api, "getAssistantItems").mockResolvedValue({ items: [], turn: null });
    const added = vi.spyOn(AbortSignal.prototype, "addEventListener");
    const removed = vi.spyOn(AbortSignal.prototype, "removeEventListener");
    vi.spyOn(api, "openConversationStream").mockImplementation(async () => {
      const body = sseBody();
      body.send(": ping\n\n");
      body.end();
      return body.body;
    });
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    view = await mount(<Probe id={7} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    const aborts = (spy: typeof added) =>
      spy.mock.calls.filter(([type]) => type === "abort").length;
    expect(aborts(added)).toBeGreaterThan(10);
    // One wait between attempts holds one; nothing else accumulates.
    expect(aborts(added) - aborts(removed)).toBeLessThanOrEqual(2);
  });

  test("a turn event updates turn status", async () => {
    vi.spyOn(api, "getAssistantItems").mockResolvedValue({
      items: [],
      turn: chatTurn(9, "queued"),
    });
    const stream = sseBody();
    streams(stream);
    view = await mount(<Probe id={7} />);
    await settle();
    expect(current().turn).toMatchObject({ id: 9, status: "queued" });

    stream.turn(chatTurn(9, "running"));
    await settle();
    expect(current().turn).toMatchObject({ id: 9, status: "running" });

    stream.turn(chatTurn(9, "done", { cost_usd: "0.0400" }));
    await settle();
    expect(current().turn).toMatchObject({ id: 9, status: "done", cost_usd: "0.0400" });
  });

  test("another conversation shows only its own items, and the old stream is closed", async () => {
    vi.spyOn(api, "getAssistantItems").mockImplementation(async (id) => ({
      items: [chatItem(id, 1, "user_text", { text: `conversation ${id}` })],
      turn: null,
    }));
    const open = streams(sseBody(), sseBody());
    view = await mount(<Probe id={7} />);
    await settle();
    expect(current().items).toHaveLength(1);
    const signal = open.mock.calls[0]?.[2];

    await view.rerender(<Probe id={8} />);
    await settle();
    expect(signal?.aborted).toBe(true);
    expect(current().items.map((item) => item.payload.text)).toEqual(["conversation 8"]);
    expect(open).toHaveBeenLastCalledWith(8, 1, expect.any(AbortSignal));
  });

  test("no conversation reads nothing", async () => {
    const items = vi.spyOn(api, "getAssistantItems");
    const open = vi.spyOn(api, "openConversationStream");
    view = await mount(<Probe id={null} />);
    await settle();
    expect(items).not.toHaveBeenCalled();
    expect(open).not.toHaveBeenCalled();
    expect(current()).toEqual({ items: [], turn: null, connected: false, gone: false });
  });
});
