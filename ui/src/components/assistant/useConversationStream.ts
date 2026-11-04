/**
 * One conversation as it stands, followed live: its items and its turn.
 *
 * It reads the items once (`GET …/items`), then holds the server-sent stream open, which resends
 * every item written or rewritten past the highest `rev` it has seen. A stream that ends or fails
 * is opened again, 1 s after the first loss and twice as long after each one in a row, up to 15 s,
 * with `Last-Event-ID` so nothing is missed and nothing is drawn twice. Only a stream that sent
 * something (an item, a turn, a heartbeat) counts as having held: one that opens and closes at
 * once backs off like a failure. A conversation the API will not show this reader (404, 401,
 * 403) is not asked for again: the stream says it is `gone`.
 *
 * An item is one row however often it is rewritten: the copy with the highest `rev` wins. The
 * items keep the order they were made in (their `id`), not their `rev`, which moves on every
 * rewrite: a proposal approved an hour later would otherwise jump to the foot of the
 * conversation.
 */

import { useEffect, useState } from "react";
import { ApiError, api } from "../../api/client";
import type { ChatItem, ChatTurn } from "../../api/types";
import { parseSse } from "./sse";

const FIRST_WAIT_MS = 1000;
const LONGEST_WAIT_MS = 15_000;

type Stream = { items: ChatItem[]; turn: ChatTurn | null; connected: boolean; gone: boolean };

const NOTHING: Stream = { items: [], turn: null, connected: false, gone: false };

const REFUSED = new Set([401, 403, 404]);

function refused(error: unknown): boolean {
  return error instanceof ApiError && REFUSED.has(error.status);
}

/** A turn the conversation is waiting on: one turn runs per conversation, so nothing else may
 *  start until it ends. */
export function isLive(turn: ChatTurn | null): turn is ChatTurn & { status: "queued" | "running" } {
  return turn !== null && (turn.status === "queued" || turn.status === "running");
}

function merged(held: ChatItem[], incoming: ChatItem[]): ChatItem[] {
  const byId = new Map(held.map((item) => [item.id, item]));
  for (const item of incoming) {
    const seen = byId.get(item.id);
    if (!seen || item.rev > seen.rev) byId.set(item.id, item);
  }
  return [...byId.values()].sort((a, b) => a.id - b.id);
}

function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    };
    const timer = setTimeout(done, ms);
    signal.addEventListener("abort", done, { once: true });
  });
}

export function useConversationStream(conversationId: number | null): Stream {
  const [state, setState] = useState<{ id: number | null; stream: Stream }>({
    id: conversationId,
    stream: NOTHING,
  });

  useEffect(() => {
    setState({ id: conversationId, stream: NOTHING });
    if (conversationId === null) return;
    const stop = new AbortController();
    const { signal } = stop;
    let highest = 0;

    const update = (change: (stream: Stream) => Stream) =>
      setState((current) =>
        current.id === conversationId
          ? { id: current.id, stream: change(current.stream) }
          : current,
      );
    const take = (items: ChatItem[]) => {
      for (const item of items) highest = Math.max(highest, item.rev);
      update((stream) => ({ ...stream, items: merged(stream.items, items) }));
    };
    const setTurn = (turn: ChatTurn | null) => update((stream) => ({ ...stream, turn }));
    const giveUp = () => update((stream) => ({ ...stream, connected: false, gone: true }));

    async function follow(id: number) {
      try {
        const seed = await api.getAssistantItems(id);
        if (signal.aborted) return;
        take(seed.items);
        setTurn(seed.turn);
      } catch (error) {
        if (refused(error)) return giveUp();
        // The stream sends the same rows; any other failed first read only means waiting for it.
      }
      let wait = FIRST_WAIT_MS;
      while (!signal.aborted) {
        try {
          const body = await api.openConversationStream(id, highest, signal);
          update((stream) => ({ ...stream, connected: true }));
          // lib.dom types the decoder's input as any BufferSource, which a byte stream is.
          const decoder = new TextDecoderStream() as TransformStream<Uint8Array, string>;
          const reader = body.pipeThrough(decoder).getReader();
          const cancel = () => void reader.cancel().catch(() => {});
          signal.addEventListener("abort", cancel, { once: true });
          let carry = "";
          try {
            for (;;) {
              const { value, done } = await reader.read();
              if (done || signal.aborted) break;
              wait = FIRST_WAIT_MS;
              const parsed = parseSse(value, carry);
              carry = parsed.carry;
              for (const message of parsed.messages) {
                if (message.event === "item") take([JSON.parse(message.data) as ChatItem]);
                else if (message.event === "turn") setTurn(JSON.parse(message.data) as ChatTurn);
              }
            }
          } finally {
            signal.removeEventListener("abort", cancel);
          }
        } catch (error) {
          if (refused(error)) return giveUp();
          // A lost connection, or any other refusal: the answer is to wait and ask again.
        }
        if (signal.aborted) return;
        update((stream) => ({ ...stream, connected: false }));
        await pause(wait, signal);
        wait = Math.min(wait * 2, LONGEST_WAIT_MS);
      }
    }

    follow(conversationId);
    return () => stop.abort();
  }, [conversationId]);

  return state.id === conversationId ? state.stream : NOTHING;
}
