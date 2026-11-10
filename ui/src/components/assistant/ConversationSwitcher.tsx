/**
 * The Drawer's header actions: New chat, and the history of every conversation in a popup, the
 * open one marked, searchable by title, each row with its last activity. Any row deletes from
 * there: its trash turns the row itself into the question, so the confirmation sits on the thing
 * it is about and asks nothing of a dialog over the Drawer. A conversation takes its first
 * message as its title; one with nothing said yet is a new conversation.
 *
 * The history shuts when its list empties and the focus goes to New chat, so a list that fills
 * again (the next send makes a conversation) never reopens it under the person typing.
 */

import { type RefObject, useEffect, useId, useRef, useState } from "react";
import type { ChatConversation } from "../../api/types";
import { formatRelative } from "../../format";
import { Button, Icon, IconButton, Popup, Pressable, TextField } from "../ui";

/** Each conversation's words, made distinct: a row's trash is named by them. */
function conversationLabels(conversations: ChatConversation[]): Map<number, string> {
  const seen = new Map<string, number>();
  const labels = new Map<number, string>();
  for (const conversation of conversations) {
    const title = conversation.title?.trim() || "New conversation";
    const count = (seen.get(title) ?? 0) + 1;
    seen.set(title, count);
    labels.set(conversation.id, count === 1 ? title : `${title} (${count})`);
  }
  return labels;
}

export function ConversationSwitcher({
  conversations,
  currentId,
  isCreating = false,
  onSelect,
  onNew,
  onDelete,
}: {
  conversations: ChatConversation[] | undefined;
  currentId: number | null;
  isCreating?: boolean;
  onSelect: (id: number) => void;
  onNew: () => void;
  onDelete: (id: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement | null>(null);
  const newChat = useRef<HTMLButtonElement>(null);
  const searchField = useRef<HTMLInputElement>(null);
  const all = conversations ?? [];
  if (open && all.length === 0) setOpen(false);
  return (
    <>
      <IconButton
        ref={newChat}
        icon="plus"
        label="New chat"
        isLoading={isCreating}
        onClick={onNew}
      />
      {all.length > 0 && (
        <Popup
          isOpen={open}
          onClose={() => setOpen(false)}
          label="Conversation history"
          placement="bottom-end"
          initialFocusRef={searchField}
          trigger={({ ref, ...aria }) => (
            <IconButton
              {...aria}
              ref={(node) => {
                button.current = node;
                if (typeof ref === "function") ref(node);
                else if (ref) ref.current = node;
              }}
              icon="history"
              label="Conversation history"
              isSelected={open}
              onClick={() => setOpen((value) => !value)}
            />
          )}
        >
          <History
            searchField={searchField}
            conversations={all}
            currentId={currentId}
            onSelect={(id) => {
              button.current?.focus();
              setOpen(false);
              onSelect(id);
            }}
            onDelete={(id) => {
              if (all.length === 1) {
                newChat.current?.focus();
                setOpen(false);
              }
              onDelete(id);
            }}
          />
        </Popup>
      )}
    </>
  );
}

function History({
  searchField,
  conversations,
  currentId,
  onSelect,
  onDelete,
}: {
  searchField: RefObject<HTMLInputElement | null>;
  conversations: ChatConversation[];
  currentId: number | null;
  onSelect: (id: number) => void;
  onDelete: (id: number) => void;
}) {
  const [query, setQuery] = useState("");
  const [asking, setAsking] = useState<number | null>(null);
  const labels = conversationLabels(conversations);
  const needle = query.trim().toLowerCase();
  const shown = conversations.filter((conversation) =>
    (labels.get(conversation.id) ?? "").toLowerCase().includes(needle),
  );

  return (
    <div className="grid w-80 max-w-full gap-2 p-2">
      <TextField
        ref={searchField}
        aria-label="Search conversations"
        placeholder="Search conversations"
        spacing="compact"
        value={query}
        onChange={(event) => {
          setQuery(event.target.value);
          setAsking(null);
        }}
        elemBeforeInput={<Icon glyph="search" size="small" />}
      />
      {shown.length === 0 ? (
        <p className="px-2 py-3 text-body-sm text-subtle">
          No conversations match "{query.trim()}".
        </p>
      ) : (
        <ul className="-mx-2 -mb-2 max-h-88 overflow-y-auto overscroll-contain pb-1">
          {shown.map((conversation) => (
            <Row
              key={conversation.id}
              conversation={conversation}
              label={labels.get(conversation.id) ?? ""}
              isCurrent={conversation.id === currentId}
              isAsking={asking === conversation.id}
              onSelect={() => onSelect(conversation.id)}
              onAsk={() => setAsking(conversation.id)}
              onCancel={() => setAsking(null)}
              onDelete={() => {
                // Off the row before it goes, and still inside the popup.
                searchField.current?.focus();
                setAsking(null);
                onDelete(conversation.id);
              }}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function Row({
  conversation,
  label,
  isCurrent,
  isAsking,
  onSelect,
  onAsk,
  onCancel,
  onDelete,
}: {
  conversation: ChatConversation;
  label: string;
  isCurrent: boolean;
  isAsking: boolean;
  onSelect: () => void;
  onAsk: () => void;
  onCancel: () => void;
  onDelete: () => void;
}) {
  const questionId = useId();
  const cancel = useRef<HTMLButtonElement>(null);
  const trash = useRef<HTMLButtonElement>(null);
  const backToTrash = useRef(false);

  // The trash and Cancel replace each other under the focus, which would otherwise drop out of
  // the popup.
  useEffect(() => {
    if (isAsking) cancel.current?.focus();
    else if (backToTrash.current) trash.current?.focus();
    backToTrash.current = false;
  }, [isAsking]);

  function withdraw() {
    backToTrash.current = true;
    onCancel();
  }

  if (isAsking) {
    return (
      <li
        className="flex items-center gap-2 py-1 pr-2 pl-3"
        onKeyDown={(event) => {
          if (event.key !== "Escape") return;
          // Spent on the row, so the layer stack leaves the popup open.
          event.preventDefault();
          withdraw();
        }}
      >
        {/* biome-ignore lint/a11y/useSemanticElements: a question and its two answers, not a form's fieldset */}
        <div
          role="group"
          aria-labelledby={questionId}
          className="flex min-w-0 flex-1 items-center gap-2"
        >
          <span id={questionId} className="min-w-0 flex-1 truncate text-body text-danger">
            Delete "{label}"?
          </span>
          <Button ref={cancel} spacing="compact" onClick={withdraw}>
            Cancel
          </Button>
          <Button appearance="danger" spacing="compact" onClick={onDelete}>
            Delete
          </Button>
        </div>
      </li>
    );
  }

  return (
    <li className="group relative flex items-center">
      <Pressable
        appearance="subtle"
        isSelected={isCurrent}
        aria-current={isCurrent ? "true" : undefined}
        onClick={onSelect}
        className="focus-inset flex min-w-0 flex-1 items-center gap-3 py-1.5 pr-3 pl-3 text-body"
      >
        <span data-title="" className="min-w-0 flex-1 truncate">
          {label}
        </span>
        <time
          dateTime={conversation.updated_at}
          className="shrink-0 text-body-sm whitespace-nowrap text-subtlest group-focus-within:opacity-0 group-hover:opacity-0 pointer-coarse:opacity-0"
        >
          {formatRelative(conversation.updated_at)}
        </time>
      </Pressable>
      {/* Always in the tab order, but out of sight and out of the pointer's way until the row is
          hovered or holds the focus; a touch screen has no hover, so there it always shows. */}
      <span className="pointer-events-none absolute right-2 opacity-0 group-focus-within:pointer-events-auto group-focus-within:opacity-100 group-hover:pointer-events-auto group-hover:opacity-100 pointer-coarse:pointer-events-auto pointer-coarse:opacity-100">
        <IconButton
          ref={trash}
          icon="trash"
          label={`Delete ${label}`}
          spacing="compact"
          onClick={onAsk}
        />
      </span>
    </li>
  );
}
