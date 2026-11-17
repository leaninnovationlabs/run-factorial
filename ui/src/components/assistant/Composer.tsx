/**
 * Where a person writes to the assistant. Enter sends; Shift+Enter, Option+Enter (Alt+Enter) and
 * Ctrl+Enter each start a new line, since people bring the habit of one chat app or another. One turn
 * runs per conversation, so while one is queued or running the field waits and Stop takes Send's
 * place; the thread's typing dots say the turn is under way, so no line here repeats it. A line
 * shows only for what the dots would hide: a queued turn nobody has claimed for 10 seconds (the
 * assistant's process is not running), and a turn the API knows of before the stream shows it.
 *
 * Send and Stop are one button, made unavailable with `aria-disabled` rather than `disabled`, so
 * a keyboard press leaves the focus on it; a disabled field drops the focus, so when the turn ends
 * it goes back to the field unless the person has since pressed or focused somewhere else.
 *
 * Over the field wait what the person pointed at and the files they attached, as chips, each
 * with a way out; the paperclip picks a file and a paste of one attaches it (`files.ts`).
 *
 * The draft stays until the API takes it: a refused send keeps what was written, and says why.
 * The assistant's provider holds the draft (`draft.ts`), so it outlives the frame this is drawn
 * in; with no holder given, the composer keeps its own.
 */

import { type FocusEvent, type KeyboardEvent, type Ref, useEffect, useRef, useState } from "react";
import type { ChatTurn } from "../../api/types";
import { ActionError, cx, IconButton, TextArea } from "../ui";
import { type Draft, EMPTY_DRAFT, isEmpty } from "./draft";
import { attach, MAX_FILES } from "./files";
import { useBodyText } from "./frames";
import { FileChips, PointedChips } from "./PointedChips";
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
  draft,
  onDraft,
  onSend,
  onStop,
  inputRef,
  compact = false,
  acceptsImages = true,
}: {
  turn: ChatTurn | null;
  /** The API said a turn is already under way here, before the stream has shown it. */
  isBlocked: boolean;
  error: unknown;
  /** The draft and where a change to it goes, when something outside holds it. */
  draft?: Draft;
  onDraft?: (next: Draft | ((was: Draft) => Draft)) => void;
  /** True once the API has taken the message. */
  onSend: (draft: Draft) => Promise<boolean>;
  /** Settles when the API has answered; a refusal makes Stop pressable again. */
  onStop: (turnId: number) => Promise<unknown>;
  inputRef?: Ref<HTMLTextAreaElement>;
  /** A frame beside the page: one row that grows with its lines, and no line under it. */
  compact?: boolean;
  /** Whether the assistant's backend takes an image; a text file always goes. */
  acceptsImages?: boolean;
}) {
  const body = useBodyText();
  const picker = useRef<HTMLInputElement>(null);
  /** Why the last file picked could not be attached. */
  const [refusedFile, setRefusedFile] = useState<string | null>(null);
  const [own, setOwn] = useState(EMPTY_DRAFT);
  const held = draft ?? own;
  const hold = onDraft ?? setOwn;
  const text = held.text;
  const setText = (written: string) => hold({ ...held, text: written });
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

  // Both of these wait, on a file's bytes or on the API, and the draft can change meanwhile:
  // something is pointed at, a second paste lands. So each writes onto the draft as it then
  // stands, never onto the one it started from.
  async function add(picked: File[]) {
    if (picked.length === 0) return;
    const { added, refused } = await attach(picked, held.files, acceptsImages);
    setRefusedFile(refused);
    if (added.length === 0) return;
    hold((was) => ({ ...was, files: [...was.files, ...added].slice(0, MAX_FILES) }));
  }

  async function send() {
    if (isEmpty(held) || busy || sending) return;
    const sent = held;
    setSending(true);
    const taken = await onSend({ ...sent, text: text.trim() });
    setSending(false);
    if (!taken) return;
    // What was sent goes; what joined the draft while the request was out stays.
    hold((was) => ({
      text: was.text === sent.text ? "" : was.text,
      pointed: was.pointed.filter((item) => !sent.pointed.includes(item)),
      files: was.files.filter((file) => !sent.files.includes(file)),
    }));
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    if (event.altKey || event.ctrlKey) {
      // The browser writes a newline for Shift+Enter alone, so these ask it for the same edit:
      // over any selection, the caret after it and in view, and one step of the field's undo.
      // Assigning `value` does none of the last two, so it is only for a browser without the
      // command.
      if (!document.execCommand?.("insertText", false, "\n")) {
        const field = event.currentTarget;
        const { selectionStart: start, selectionEnd: end, value } = field;
        field.value = `${value.slice(0, start)}\n${value.slice(end)}`;
        field.setSelectionRange(start + 1, start + 1);
        setText(field.value);
      }
      return;
    }
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
      {refusedFile && (
        <p role="alert" className="text-body-sm text-danger">
          {refusedFile}
        </p>
      )}
      <FileChips
        files={held.files}
        onRemove={(at) => hold({ ...held, files: held.files.filter((_, index) => index !== at) })}
      />
      <PointedChips
        pointed={held.pointed}
        onRemove={(at) =>
          hold({ ...held, pointed: held.pointed.filter((_, index) => index !== at) })
        }
      />
      <div className="flex items-stretch gap-2">
        <TextArea
          ref={inputRef}
          aria-label="Message the assistant"
          className={cx("min-w-0 flex-1", body)}
          minimumRows={compact ? Math.min(6, text.split("\n").length) : 2}
          resize="none"
          placeholder={
            compact
              ? "Reply"
              : "Ask about tickets, runs, costs or a process. Shift+Enter for a new line."
          }
          value={text}
          disabled={busy}
          // The words are on their way: they are not to be changed under the request.
          readOnly={sending}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={onKeyDown}
          // A screenshot is pasted more often than it is saved and picked. A paste that carries
          // words is the words: a spreadsheet's cells come with a picture of themselves.
          onPaste={(event) => {
            const pasted = [...(event.clipboardData?.files ?? [])];
            if (pasted.length === 0 || event.clipboardData?.getData("text/plain")) return;
            event.preventDefault();
            void add(pasted);
          }}
          {...tracksFocus}
        />
        <input
          ref={picker}
          type="file"
          multiple
          hidden
          tabIndex={-1}
          onChange={(event) => {
            const picked = [...(event.currentTarget.files ?? [])];
            // The same file can be picked again after it was taken out.
            event.currentTarget.value = "";
            void add(picked);
          }}
        />
        <IconButton
          icon="paperclip"
          label="Attach a file"
          className={TALL}
          aria-disabled={busy || undefined}
          onClick={() => {
            if (!busy) picker.current?.click();
          }}
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
      {!compact && (
        <p className="text-center text-body-sm text-subtlest">
          Factorial Assistant can make mistakes. Check important results.
        </p>
      )}
    </div>
  );
}
