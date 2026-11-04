/**
 * The conversation, item by item, each drawn by its kind: your text, the assistant's markdown, its
 * tool calls, and the cards its tools drew. A kind this build does not know is skipped, so an
 * older tab survives a newer server.
 *
 * Drawn as who spoke: each message of yours is a bubble on the right, and everything the
 * assistant did in reply is one block on the left, without a bubble, so a table or a chart has
 * the column's full width. A run of tool calls in a reply is one row (`ToolGroup`), opened and
 * shut by call here, since a call that drew a card leaves its run and can remount the row. While
 * the turn is live and no text is arriving, the reply shows the assistant typing, and the
 * stream's status region, always present so a screen reader hears it fill, says so.
 *
 * The stream follows its foot while the reader is there, and stays where they scrolled to when
 * they go up (`activity/follow.ts`). It owns its scrolling, so the Drawer can open again where
 * the reader left it after a Show me.
 */

import { type MutableRefObject, useLayoutEffect, useRef, useState } from "react";
import type { ChatItem, ChatTurn, ProposalPayload } from "../../api/types";
import { atFoot } from "../activity/follow";
import { EmptyState, VisuallyHidden } from "../ui";
import { AssistantAvatar, PersonAvatar } from "./Avatar";
import { AssistantText } from "./items/AssistantText";
import { ChartCard } from "./items/ChartCard";
import { EditorDraftChip } from "./items/EditorDraftChip";
import { Notice } from "./items/Notice";
import { ProposalCard } from "./items/ProposalCard";
import { ShowMeChip } from "./items/ShowMeChip";
import { ToolGroup } from "./items/ToolGroup";
import { namesCard } from "./items/ToolLine";
import { TypingIndicator } from "./TypingIndicator";
import { isLive } from "./useConversationStream";

type Retry = (text: string, page: string) => void;

type Part = { tools: false; item: ChatItem } | { tools: true; key: number; calls: ChatItem[] };

/** A reply is keyed by the message it answers, so the block the typing dots open is the same one
 *  the reply's first item lands in. */
type Block = { person: true; item: ChatItem } | { person: false; key: string; parts: Part[] };

const text = (item: ChatItem) => (typeof item.payload.text === "string" ? item.payload.text : "");

function drawn(item: ChatItem): boolean {
  switch (item.kind) {
    case "user_text":
    case "show_me":
    case "chart":
    case "editor_draft":
    case "notice":
      return true;
    case "assistant_text":
      return text(item) !== "";
    case "tool":
      return !namesCard(item.payload.summary);
    case "proposal":
      // Written empty, then drawn once the proposal has its id, in the same transaction.
      return typeof item.payload.proposal_id === "number";
    default:
      return false;
  }
}

function blocksOf(items: ChatItem[], replying: boolean): Block[] {
  const blocks: Block[] = [];
  const reply = () => {
    const last = blocks.at(-1);
    if (last && !last.person) return last;
    const opened: Extract<Block, { person: false }> = {
      person: false,
      key: `reply-${last?.person ? last.item.id : 0}`,
      parts: [],
    };
    blocks.push(opened);
    return opened;
  };
  for (const item of items) {
    if (item.kind === "user_text") {
      blocks.push({ person: true, item });
      continue;
    }
    const { parts } = reply();
    const last = parts.at(-1);
    if (item.kind !== "tool") parts.push({ tools: false, item });
    else if (last?.tools) last.calls.push(item);
    else parts.push({ tools: true, key: item.id, calls: [item] });
  }
  if (replying) reply();
  return blocks;
}

/** Live, and nothing on screen is moving: no reply yet, or its last part is not text still being
 *  written. */
function typing(shown: ChatItem[], turn: ChatTurn | null): boolean {
  if (!isLive(turn)) return false;
  const last = shown.at(-1);
  return last?.kind !== "assistant_text" || last.final;
}

function Item({
  item,
  items,
  onRetry,
  retryDisabled,
}: {
  item: ChatItem;
  items: ChatItem[];
  onRetry: Retry;
  retryDisabled: boolean;
}) {
  const { payload } = item;
  switch (item.kind) {
    case "assistant_text":
      return <AssistantText text={text(item)} />;
    case "proposal":
      return <ProposalCard payload={payload as unknown as ProposalPayload} />;
    case "show_me":
      return <ShowMeChip payload={payload} />;
    case "chart":
      return <ChartCard payload={payload} />;
    case "editor_draft":
      return <EditorDraftChip item={item} />;
    case "notice":
      return <Notice item={item} items={items} onRetry={onRetry} retryDisabled={retryDisabled} />;
    default:
      return null;
  }
}

function Said({ item }: { item: ChatItem }) {
  return (
    <div data-speaker="person" className="flex items-start justify-end gap-2">
      <p className="max-w-[85%] min-w-0 rounded-lg bg-neutral px-3 py-2 text-body break-words whitespace-pre-wrap text-default">
        <VisuallyHidden>You said </VisuallyHidden>
        {text(item)}
      </p>
      <PersonAvatar />
    </div>
  );
}

export function ItemStream({
  items,
  turn,
  onRetry,
  scrollTop,
}: {
  items: ChatItem[];
  turn: ChatTurn | null;
  onRetry: Retry;
  /** Where the reader left the stream, kept across the Drawer closing; null for its foot, which
   *  is where they return to however much has arrived since. */
  scrollTop: MutableRefObject<number | null>;
}) {
  const log = useRef<HTMLDivElement>(null);
  /** The place to return to, held until the items are drawn: the Drawer mounts before its
   *  conversation is read again, and an empty box can only sit at its top. */
  const returning = useRef(scrollTop.current);
  const following = useRef(scrollTop.current === null);
  const [opened, setOpened] = useState<ReadonlySet<number>>(new Set());
  const toggle = (calls: ChatItem[]) =>
    setOpened((was) => {
      const now = new Set(was);
      const open = isOpen(calls, was);
      for (const call of calls) {
        if (open) now.delete(call.id);
        else now.add(call.id);
      }
      return now;
    });

  const shown = items.filter(drawn);
  const replying = typing(shown, turn);
  const blocks = blocksOf(shown, replying);

  // biome-ignore lint/correctness/useExhaustiveDependencies: the dots arriving grow the stream as an item does
  useLayoutEffect(() => {
    const element = log.current;
    if (!element || items.length === 0) return;
    if (returning.current !== null) {
      element.scrollTop = returning.current;
      returning.current = null;
      following.current = atFoot(element);
    } else if (following.current) {
      element.scrollTop = element.scrollHeight;
    }
  }, [items, replying]);

  return (
    // biome-ignore lint/a11y/useSemanticElements: the scroll box, named; the items are the content
    <div
      ref={log}
      data-item-stream
      role="region"
      aria-label="Conversation"
      // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrolling region must be reachable by keyboard
      tabIndex={0}
      onScroll={(event) => {
        following.current = atFoot(event.currentTarget);
        scrollTop.current = following.current ? null : event.currentTarget.scrollTop;
      }}
      className="focus-inset h-full overflow-y-auto overscroll-contain px-3 py-4 md:px-5"
    >
      {items.length === 0 ? (
        <EmptyState
          header="Ask Factorial Assistant"
          description="Ask which tickets are blocked, what a run cost, where to find a control, or how to change a process."
          headingLevel={3}
          width="narrow"
        />
      ) : (
        // A reading width down the middle: the page is the whole window wide.
        <div className="mx-auto flex max-w-4xl flex-col gap-3">
          {blocks.map((block, at) =>
            block.person ? (
              <Said key={block.item.id} item={block.item} />
            ) : (
              <div key={block.key} data-speaker="assistant" className="flex items-start gap-2">
                <AssistantAvatar />
                <div className="flex min-w-0 flex-1 flex-col gap-3">
                  {block.parts.length > 0 && (
                    <VisuallyHidden>Factorial Assistant said</VisuallyHidden>
                  )}
                  {block.parts.map((part) =>
                    part.tools ? (
                      <ToolGroup
                        key={key(part)}
                        calls={part.calls}
                        open={isOpen(part.calls, opened)}
                        onToggle={() => toggle(part.calls)}
                      />
                    ) : (
                      <Item
                        key={key(part)}
                        item={part.item}
                        items={items}
                        onRetry={onRetry}
                        retryDisabled={isLive(turn)}
                      />
                    ),
                  )}
                  {replying && at === blocks.length - 1 && <TypingIndicator />}
                </div>
              </div>
            ),
          )}
        </div>
      )}
      <div role="status">
        {replying && <VisuallyHidden>Factorial Assistant is replying</VisuallyHidden>}
      </div>
    </div>
  );
}

function isOpen(calls: ChatItem[], opened: ReadonlySet<number>): boolean {
  return calls.some((call) => opened.has(call.id));
}

function key(part: Part): number {
  return part.tools ? part.key : part.item.id;
}
