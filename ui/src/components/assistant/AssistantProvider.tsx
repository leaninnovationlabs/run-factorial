/**
 * The assistant's place in the shell. In the organization it is a page of its own, `/assistant`
 * (`AssistantPage`); inside a project it is the modal Drawer over the project's page, opened from
 * the project's navigation. Both draw one conversation (`AssistantChat`), and the provider holds
 * what has to outlive either — which conversation is open and where it was scrolled, the page the
 * reader is asking about, and the Show me hand-off.
 *
 * Show me goes to the card's page and points at the control there (`showMe.tsx`); Back to chat
 * returns to the frame the chip was pressed in — the page, or the Drawer over the card's page — on
 * the same conversation, scrolled where it was. Only a person's click moves them: the model can
 * only offer the chip.
 *
 * A message is sent with the page the reader is asking about as its context: the page behind the
 * Drawer, or the last one visited before the assistant's own page — never the assistant's page.
 *
 * The Drawer is a chunk of its own, loaded the first time it opens: the markdown it renders
 * replies with is the largest thing it brings, and most visits never open it.
 */

import {
  createContext,
  lazy,
  type MouseEvent,
  type MutableRefObject,
  type ReactNode,
  Suspense,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import type { ShowMePayload } from "../../api/types";
import { ShowMeSpotlight, showMeHref } from "./showMe";

export const ASSISTANT_PATH = "/assistant";

const AssistantChat = lazy(() =>
  import("./AssistantChat").then((m) => ({ default: m.AssistantChat })),
);

type Assistant = {
  /** Go to the assistant's page. */
  open: () => void;
  /** Open the Drawer over the page the reader is on. */
  openDrawer: () => void;
  /** Close the Drawer, if it is open. */
  close: () => void;
  showMe: (card: ShowMePayload) => void;
};

/** What the assistant's page reads and writes across visits. */
export type AssistantSession = {
  conversationId: number | null;
  onConversation: (id: number | null) => void;
  scrollTop: MutableRefObject<number | null>;
  /** The dashboard page a message is about: the last one visited that was not the assistant's. */
  contextPage: MutableRefObject<string | null>;
};

const AssistantContext = createContext<(Assistant & { session: AssistantSession }) | null>(null);

function useAssistantContext() {
  const assistant = useContext(AssistantContext);
  if (!assistant) throw new Error("useAssistant needs an AssistantProvider above it");
  return assistant;
}

export function useAssistant(): Assistant {
  return useAssistantContext();
}

export function useAssistantSession(): AssistantSession {
  return useAssistantContext().session;
}

export function AssistantProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const [conversationId, setConversationId] = useState<number | null>(null);
  const [isDrawerOpen, setDrawerOpen] = useState(false);
  const drawerOpen = useRef(false);
  drawerOpen.current = isDrawerOpen;
  const scrollTop = useRef<number | null>(null);
  // Null until a page is visited: a conversation opened straight onto /assistant is about none.
  const contextPage = useRef<string | null>(pathname === ASSISTANT_PATH ? null : pathname);
  /** The card being shown; `key` is new for every press, so a second press points afresh.
   *  `fromDrawer` is the frame Back to chat returns to. */
  const [shown, setShown] = useState<{
    card: ShowMePayload;
    key: number;
    fromDrawer: boolean;
  } | null>(null);
  const go = useRef(navigate);
  useEffect(() => {
    go.current = navigate;
  }, [navigate]);
  if (pathname !== ASSISTANT_PATH) contextPage.current = pathname;

  const assistant = useMemo<Assistant>(
    () => ({
      open: () => {
        setShown(null);
        setDrawerOpen(false);
        go.current(ASSISTANT_PATH);
      },
      openDrawer: () => {
        setShown(null);
        setDrawerOpen(true);
      },
      close: () => setDrawerOpen(false),
      showMe: (card) => {
        const fromDrawer = drawerOpen.current;
        setDrawerOpen(false);
        setShown((previous) => ({ card, key: (previous?.key ?? 0) + 1, fromDrawer }));
        go.current(showMeHref(card));
      },
    }),
    [],
  );
  const session = useMemo<AssistantSession>(
    () => ({ conversationId, onConversation: setConversationId, scrollTop, contextPage }),
    [conversationId],
  );

  // A page the person went to themselves ends the pointing: the control is not on it.
  const shownPath = shown?.card.path;
  // biome-ignore lint/correctness/useExhaustiveDependencies: a new page is the event, not the card
  useEffect(() => {
    if (shownPath !== undefined && pathname !== shownPath) setShown(null);
  }, [pathname]);

  return (
    <AssistantContext.Provider value={{ ...assistant, session }}>
      {children}
      {isDrawerOpen && (
        <Suspense fallback={null}>
          <AssistantChat
            variant="drawer"
            conversationId={conversationId}
            onConversation={setConversationId}
            scrollTop={scrollTop}
            contextPage={contextPage}
            onClose={assistant.close}
          />
        </Suspense>
      )}
      {shown && !isDrawerOpen && (
        <ShowMeSpotlight
          key={shown.key}
          card={shown.card}
          onBack={shown.fromDrawer ? assistant.openDrawer : assistant.open}
          onDismiss={() => setShown(null)}
        />
      )}
    </AssistantContext.Provider>
  );
}

/** A path on this dashboard. `//host` and `/\host` start with a slash and are another site to a
 *  browser, so they are not. */
export function isDashboardPath(url: string): boolean {
  return url.startsWith("/") && !/^\/[/\\]/.test(url);
}

/**
 * A link to a dashboard page from inside the conversation, followed by the router. In the Drawer,
 * which is modal and would stay over the page the link opens, following it closes the Drawer
 * first. Never blue: the text's own colour, underlined.
 */
export function DashboardLink({ to, children }: { to: string; children: ReactNode }) {
  const { close } = useAssistant();
  return (
    <Link
      to={to}
      onClick={(event: MouseEvent) => {
        if (!event.metaKey && !event.ctrlKey && !event.shiftKey) close();
      }}
      className="font-medium underline underline-offset-2"
    >
      {children}
    </Link>
  );
}
