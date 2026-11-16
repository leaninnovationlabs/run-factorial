/**
 * Settings' Credential check: one real turn in an archived conversation of its own, followed on
 * that conversation's stream until the turn ends or a minute passes. The time is the client's, from the
 * press to the turn's end, and the reply is the newest copy of each of the assistant's items.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import type { ChatItem, ChatTurn } from "../../api/types";
import { isLive, useConversationStream } from "../assistant/useConversationStream";

const GIVE_UP_MS = 60_000;

export type ConnectionTestState =
  | { phase: "idle" }
  | { phase: "running" }
  | { phase: "failed"; error: unknown }
  | {
      phase: "finished";
      status: ChatTurn["status"];
      reply: string;
      elapsedMs: number;
      costUsd: string | null;
      isEstimate: boolean;
      failure: string | null;
      /** The closed `TurnFailure` kind: `auth` is a credential the model provider refused. */
      failureReason: string | null;
    };

/** The stream holds each item once, at its newest copy, in the order the items were made. */
function replyOf(items: ChatItem[]): string {
  return items
    .filter((item) => item.kind === "assistant_text")
    .map((item) => String(item.payload.text ?? ""))
    .join("\n\n")
    .trim();
}

export function useConnectionTest() {
  const [state, setState] = useState<ConnectionTestState>({ phase: "idle" });
  const [following, setFollowing] = useState<{ conversationId: number; started: number } | null>(
    null,
  );
  const presses = useRef(0);
  useEffect(() => () => void presses.current++, []);
  const { items, turn } = useConversationStream(following?.conversationId ?? null);

  const run = useCallback(async () => {
    const press = ++presses.current;
    const started = Date.now();
    setFollowing(null);
    setState({ phase: "running" });
    try {
      const { conversation_id } = await api.testAssistant();
      if (presses.current === press) setFollowing({ conversationId: conversation_id, started });
    } catch (error) {
      if (presses.current === press) {
        setState({ phase: "failed", error });
      }
    }
  }, []);

  useEffect(() => {
    if (!following || !turn || isLive(turn)) return;
    setFollowing(null);
    setState({
      phase: "finished",
      status: turn.status,
      reply: replyOf(items),
      elapsedMs: Date.now() - following.started,
      costUsd: turn.cost_usd,
      isEstimate: turn.cost_source === "estimated",
      failure: turn.status === "done" ? null : (turn.failure_message ?? turn.status),
      failureReason: turn.status === "done" ? null : turn.failure_reason,
    });
  }, [following, turn, items]);

  useEffect(() => {
    if (!following) return;
    const timer = setTimeout(
      () => {
        setFollowing(null);
        setState({
          phase: "failed",
          error: new Error("The assistant didn't finish the test reply within a minute."),
        });
      },
      GIVE_UP_MS - (Date.now() - following.started),
    );
    return () => clearTimeout(timer);
  }, [following]);

  return { state, run };
}
