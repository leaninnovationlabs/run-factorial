/**
 * Where a person writes to the assistant. Enter sends and Shift+Enter starts a new line. One turn
 * runs per conversation, so while one is queued or running the field waits and Stop takes Send's
 * place; the thread's typing dots say the turn is under way, so no line here repeats it. A line
 * shows only for what the dots would hide: a queued turn nobody has claimed for 10 seconds (the
 * assistant's process is not running), and a turn the API knows of before the stream shows it.
 *
 * Send and Stop are one button, made unavailable with `aria-disabled` rather than `disabled`, so
 * a keyboard press leaves the focus on it; a disabled field drops the focus, so when the turn ends
 * it goes back to the field unless the person has since pressed or focused somewhere else.
 *
 * The text stays until the API takes it: a refused send keeps what was written, and says why.
 */

import { type FocusEvent, type KeyboardEvent, type Ref, useEffect, useRef, useState } from "react";
import type { ChatTurn } from "../../api/types";
import { ActionError, IconButton, TextArea } from "../ui";
import { isLive } from "./useConversationStream";

const WAITING_AFTER_MS = 10_000;

/** Send and Stop stand as tall as the field beside them. */
const TALL = "h-auto w-10";

/** Whether the queued turn has waited `WAITING_AFTER_MS` since this page first saw it queued. */
function useStuckInQueue(turn: ChatTurn | null): boolean {
  const queued = turn?.status === "queued" ? turn.id : null;
  const [stuck, setStuck] = useState<number | null>(null);
  useEffect(() => {
    if (queued === null) return;
    const timer = setTimeout(() => setStuck(queued), WAITING_AFTER_MS);
    return () => clearTimeout(timer);
  }, [queued]);
  return queued !== null && stuck === queued;
}

export function Composer({
  turn,
  isBlocked,
  error,
  onSend,
  onStop,
  inputRef,
}: {
  turn: ChatTurn | null;
  /** The API said a turn is already under way here, before the stream has shown it. */
  isBlocked: boolean;
  error: unknown;
  /** True once the API has taken the message. */
  onSend: (text: string) => Promise<boolean>;
  /** Settles when the API has answered; a refusal makes Stop pressable again. */
  onStop: (turnId: number) => Promise<unknown>;
  inputRef?: Ref<HTMLTextAreaElement>;
}) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [stopped, setStopped] = useState<number | null>(null);
  const live = isLive(turn);
  const stuck = useStuckInQueue(turn);
  const busy = live || isBlocked;
  const stopping = live && stopped === turn.id;

  const root = useRef<HTMLDivElement>(null);
  const owesFocus = useRef(false);
  const wasBusy = useRef(busy);
  useEffect(() => {
    if (!busy) return;
    // The disabled field has already dropped the focus, so a press on the thread moves none and
    // fires no blur; the press itself is the sign the person has gone elsewhere.
    const pressed = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) owesFocus.current = false;
    };
    document.addEventListener("pointerdown", pressed, true);
    return () => document.removeEventListener("pointerdown", pressed, true);
  }, [busy]);
  useEffect(() => {
    const ended = wasBusy.current && !busy;
    wasBusy.current = busy;
    if (!ended || !owesFocus.current) return;
    const active = document.activeElement;
    if (active === null || active === document.body || root.current?.contains(active)) {
      root.current?.querySelector("textarea")?.focus();
    }
  }, [busy]);

  async function send() {
    const message = text.trim();
    if (!message || busy || sending) return;
    setSending(true);
    const taken = await onSend(message);
    setSending(false);
    if (taken) setText("");
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    void send();
  }

  const tracksFocus = {
    onFocus: () => {
      owesFocus.current = true;
    },
    onBlur: (event: FocusEvent) => {
      if (event.relatedTarget && !root.current?.contains(event.relatedTarget)) {
        owesFocus.current = false;
      }
    },
  };

  const status =
    live && stuck
      ? "The assistant hasn't started on this yet. If nothing happens soon, press Stop and try again."
      : !live && isBlocked
        ? "The assistant is answering another message in this conversation."
        : null;

  return (
    <div ref={root} className="space-y-2">
      {status && (
        <p role="status" className="text-body-sm text-subtle">
          {status}
        </p>
      )}
      <ActionError error={error} />
      <div className="flex items-stretch gap-2">
        <TextArea
          ref={inputRef}
          aria-label="Message the assistant"
          className="min-w-0 flex-1"
          minimumRows={2}
          resize="none"
          placeholder="Ask about tickets, runs, costs or a process. Shift+Enter for a new line."
          value={text}
          disabled={busy}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={onKeyDown}
          {...tracksFocus}
        />
        {live ? (
          <IconButton
            icon="stop-circle"
            label="Stop"
            appearance="primary"
            className={TALL}
            aria-disabled={stopping || undefined}
            {...tracksFocus}
            onClick={() => {
              if (stopping) return;
              setStopped(turn.id);
              onStop(turn.id).catch(() => setStopped(null));
            }}
          />
        ) : (
          <IconButton
            icon="send"
            label="Send"
            appearance="primary"
            className={TALL}
            isLoading={sending}
            aria-disabled={isBlocked || undefined}
            {...tracksFocus}
            onClick={() => void send()}
          />
        )}
      </div>
      <p className="text-center text-body-sm text-subtlest">
        Factorial Assistant can make mistakes. Check important results.
      </p>
    </div>
  );
}
