/**
 * Test doubles for the assistant: chat rows, the conversation stream as the API sends it one
 * server-sent event at a time, and a mount inside the router, the query client and the provider.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { vi } from "vitest";
import { ApiError, api } from "../../api/client";
import { ADMIN_PERMISSIONS, holding } from "../../api/organization.fixture";
import type { ChatItem, ChatTurn } from "../../api/types";
import { AuthProvider } from "../../auth";
import { AssistantPage } from "../../pages/AssistantPage";
import { mount } from "../../testDom";
import { ASSISTANT_PATH, AssistantProvider } from "./AssistantProvider";

export function chatItem(
  id: number,
  rev: number,
  kind: string,
  payload: Record<string, unknown>,
  extra: Partial<ChatItem> = {},
): ChatItem {
  return { id, rev, kind, payload, final: true, turn_id: null, ...extra };
}

export function chatTurn(id: number, status: ChatTurn["status"], extra: Partial<ChatTurn> = {}) {
  return {
    id,
    status,
    stop_reason: null,
    failure_reason: null,
    failure_message: null,
    cost_usd: null,
    cost_source: null,
    ...extra,
  } satisfies ChatTurn;
}

/** One connection's body, written by the test as the server writes `api/chat_stream.py`'s
 *  events. */
export function sseBody() {
  const encoder = new TextEncoder();
  let controller: ReadableStreamDefaultController<Uint8Array> | null = null;
  const body = new ReadableStream<Uint8Array>({
    start(opened) {
      controller = opened;
    },
  });
  const send = (text: string) => controller?.enqueue(encoder.encode(text));
  return {
    body,
    send,
    item: (item: ChatItem) =>
      send(`id: ${item.rev}\nevent: item\ndata: ${JSON.stringify(item)}\n\n`),
    turn: (turn: ChatTurn | null) => send(`event: turn\ndata: ${JSON.stringify(turn)}\n\n`),
    end: () => controller?.close(),
  };
}

export type SseBody = ReturnType<typeof sseBody>;

/** The API a conversation's frame reads, with one conversation, 8, and its stream by id. */
export function stubChatApi({
  items = [],
  turn = null,
}: {
  items?: ChatItem[];
  turn?: ChatTurn | null;
} = {}) {
  const streams = new Map<number, SseBody>();
  vi.spyOn(api, "getAssistantReady").mockResolvedValue({
    enabled: true,
    credential_set: true,
    credential_updated_at: null,
    ready_to_send: true,
  });
  vi.spyOn(api, "listConversations").mockResolvedValue([
    {
      id: 8,
      title: "Blocked tickets",
      created_at: "",
      updated_at: "2026-10-12T09:00:00Z",
      archived_at: null,
    },
  ]);
  vi.spyOn(api, "getAssistantItems").mockResolvedValue({ items, turn });
  vi.spyOn(api, "getProject").mockRejectedValue(new ApiError(404, "no such project"));
  vi.spyOn(api, "getTicket").mockRejectedValue(new ApiError(404, "no such ticket"));
  vi.spyOn(api, "openConversationStream").mockImplementation(async (id) => {
    const body = sseBody();
    streams.set(id, body);
    return body.body;
  });
  return { stream: (id: number) => streams.get(id) };
}

function Where() {
  const { pathname, search } = useLocation();
  return <output data-where>{`${pathname}${search}`}</output>;
}

/** `node` on every page of a memory router, under the provider, with `page` drawn on `path`'s
 *  route — the page a Show me lands on — and the assistant's own page on its route. Signed in by token, with no session, unless the test has
 *  stubbed the session probe itself: the stream names the person by their session's address. */
export async function mountInAssistant(
  node: ReactNode,
  {
    at = "/overview",
    path = "*",
    page = null,
    permissions = ADMIN_PERMISSIONS,
  }: { at?: string; path?: string; page?: ReactNode; permissions?: string[] } = {},
) {
  if (!vi.isMockFunction(api.authMe)) {
    vi.spyOn(api, "authMe").mockRejectedValue(new ApiError(401, "no session"));
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  holding(client, permissions);
  const view = await mount(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[at]}>
        <AuthProvider>
          <AssistantProvider>
            <Where />
            {node}
            <Routes>
              <Route path={path} element={page} />
              {path !== ASSISTANT_PATH && (
                <Route path={ASSISTANT_PATH} element={<AssistantPage />} />
              )}
              <Route path="*" element={null} />
            </Routes>
          </AssistantProvider>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return {
    view,
    client,
    where: () => view.container.querySelector("[data-where]")?.textContent ?? null,
  };
}

/**
 * happy-dom lays nothing out. This lays out every conversation box as a browser would draw it: 300
 * tall, its content 1000 tall once it holds items (300 while it shows its empty state), and its
 * scroll position clamped to what that content allows. Returns what undoes it.
 */
export function layOutStreams(): () => void {
  const positions = new WeakMap<Element, number>();
  const content = (element: Element) =>
    element.hasAttribute("data-item-stream") &&
    !element.textContent?.includes("Ask Factorial Assistant")
      ? 1000
      : 300;
  Object.defineProperty(HTMLElement.prototype, "scrollHeight", {
    configurable: true,
    get(this: HTMLElement) {
      return content(this);
    },
  });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", {
    configurable: true,
    get: () => 300,
  });
  Object.defineProperty(HTMLElement.prototype, "scrollTop", {
    configurable: true,
    get(this: HTMLElement) {
      return positions.get(this) ?? 0;
    },
    set(this: HTMLElement, value: number) {
      positions.set(this, Math.max(0, Math.min(value, content(this) - 300)));
    },
  });
  return () => {
    for (const name of ["scrollHeight", "clientHeight", "scrollTop"]) {
      Reflect.deleteProperty(HTMLElement.prototype, name);
    }
  };
}
