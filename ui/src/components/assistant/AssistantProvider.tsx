/**
 * The assistant's place in the shell. In the organization it is a page of its own, `/assistant`
 * (`AssistantPage`); inside a project it is the modal Drawer over the project's page, opened from
 * the project's navigation. The Drawer's Dock and Float take the conversation to a frame that
 * leaves the page usable (`frames.ts`): a column docked at the page's right edge, which the shell
 * draws in its row (`AssistantDock`), or a small window floating over the page's corner. Minimize,
 * in every frame, puts the conversation away as a bar, which opens the frame it came from. A
 * window too narrow to dock in floats instead.
 * Every frame draws one conversation (`AssistantChat`), and the provider holds what has to
 * outlive any of them — which conversation is open and where it was scrolled, the page the
 * reader is asking about, what they have written and not yet sent (`draft.ts`), whether the
 * Drawer is at its full width, and the Show me hand-off.
 *
 * Show me goes to the card's page and points at the control there (`showMe.tsx`); Back to chat
 * returns to the frame the chip was pressed in — the page, or the Drawer over the card's page — on
 * the same conversation, scrolled where it was. A frame beside the page stays where it is, so
 * there is nothing to go back to. Only a person's click moves them: the model can only offer the
 * chip.
 *
 * A right-click anywhere on the dashboard, or the keyboard's menu key, offers to take what was
 * under it into a conversation (`pointed.ts`): the thing waits as a chip in the draft, in the
 * frame beside the page if one is showing and in the Drawer otherwise. The browser's own menu
 * stays where a person needs it — with Shift, in a field that takes text, in the conversation
 * itself — and where a place has a menu of its own, which can carry the same two entries
 * (`usePointEntries`). It is a shortcut and no more: what it does, a person can do by opening the
 * assistant on the page and saying what they mean.
 *
 * The conversation's stream is followed here, not in the frame: a frame that takes the place of
 * another draws what is already read, and the bar knows a reply arrived with no frame open.
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
import { Link, useHref, useLocation, useNavigate } from "react-router-dom";
import { useCan } from "../../api/organizationQueries";
import { ContextMenu, type MenuEntry, showFlag } from "../ui";
import { BELOW_XL, useIsPhone, useMedia } from "../ui/media";
import { AssistantBar } from "./AssistantBar";
import { type Draft, EMPTY_DRAFT, type Pointed } from "./draft";
import { type Frame, type FramePrefs, useFramePrefs } from "./frames";
import { isDashboardPath } from "./paths";
import { capped, describeTarget, keepsNativeMenu, pointedElement } from "./pointed";
import { ShowMeSpotlight, type Shown, showMeHref } from "./showMe";
import { isBlank, type Stream, useConversationStream } from "./useConversationStream";

export const ASSISTANT_PATH = "/assistant";

/** How many things one message can point at (`api.schemas.ChatMessageIn`). */
export const MAX_POINTED = 5;

/** The ring on what the open menu is about: Show me's (`assistant.css`). */
const POINTED_AT = "data-assist-shown";

const AssistantChat = lazy(() =>
  import("./AssistantChat").then((m) => ({ default: m.AssistantChat })),
);

type Assistant = {
  /** Go to the assistant's page. */
  open: () => void;
  /** Open the Drawer over the page the reader is on. */
  openDrawer: () => void;
  /** Close whichever frame the conversation is in. */
  close: () => void;
  showMe: (card: Shown) => void;
  /** Go back to something a person pointed at: its page, and the thing ringed there when it can
   *  be found again, by its name or by the link it holds. */
  showWhere: (pointed: Pointed) => void;
  /** The frame the conversation is in. Never one beside the page on a phone, where the Drawer is
   *  the window, or on the assistant's own page, which is the conversation. */
  frame: Frame;
  /** From any frame: away, to the bar in the page's corner. */
  minimize: () => void;
  /** From the bar: back to the frame Minimize took it from. */
  restore: () => void;
  /** To the small window floating over the page. */
  float: () => void;
  /** To the column docked beside the page. */
  dock: () => void;
  /** Whether the window is wide enough for a page and the docked column beside it. */
  canDock: boolean;
  prefs: FramePrefs;
  setPrefs: (change: Partial<FramePrefs>) => void;
  /** Take something on the dashboard into a conversation, a new one or the open one: it waits
   *  as a chip in the draft, and the conversation's frame opens with the composer focused. */
  pointAt: (pointed: Pointed, mode: "new" | "add") => void;
  /** The "Assistant" group for a menu a place already has, or null for a reader who may not use
   *  the assistant. `pointed` is read when an entry is chosen. */
  pointEntries: (pointed: () => Pointed) => MenuEntry | null;
};

/** What the assistant's page reads and writes across visits. */
export type AssistantSession = {
  conversationId: number | null;
  onConversation: (id: number | null) => void;
  scrollTop: MutableRefObject<number | null>;
  /** The dashboard page a message is about: the last one visited that was not the assistant's. */
  contextPage: MutableRefObject<string | null>;
  /** What is written and not yet sent. */
  draft: Draft;
  setDraft: (next: Draft | ((was: Draft) => Draft)) => void;
  /** The Drawer at its full width, the conversation's reading cap lifted. */
  isWide: boolean;
  setWide: (wide: boolean) => void;
  /** The open conversation as it stands, followed live. */
  stream: Stream;
  /** The person asked for a new conversation: the frame must not open the latest one instead. */
  startsNew: MutableRefObject<boolean>;
  /** Something was just pointed at, and the composer is owed the focus; `pointedCount` is the
   *  event a frame already open hears. */
  wantsFocus: MutableRefObject<boolean>;
  pointedCount: number;
};

// Two contexts: the session changes with every word that arrives or is typed, and the
// navigation's items, which only open the assistant, should not be drawn again for each.
const AssistantContext = createContext<Assistant | null>(null);
const SessionContext = createContext<AssistantSession | null>(null);

export function useAssistant(): Assistant {
  const assistant = useContext(AssistantContext);
  if (!assistant) throw new Error("useAssistant needs an AssistantProvider above it");
  return assistant;
}

/** `pointEntries`, for a component that is also drawn where there is no assistant. */
export function usePointEntries(): Assistant["pointEntries"] {
  return useContext(AssistantContext)?.pointEntries ?? NO_ENTRIES;
}

const NO_ENTRIES = () => null;

export function useAssistantSession(): AssistantSession {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useAssistantSession needs an AssistantProvider above it");
  return session;
}

export function AssistantProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  // What the browser shows before a dashboard path: the app's prefix and the organization.
  const base = useHref("/").replace(/\/+$/, "");
  const [conversationId, setConversationId] = useState<number | null>(null);
  const [held, setHeld] = useState<Frame>("closed");
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [isWide, setWide] = useState(false);
  const [prefs, setPrefs] = useFramePrefs();
  const phone = useIsPhone();
  const canDock = !useMedia(BELOW_XL);
  const onOwnPage = pathname === ASSISTANT_PATH;
  const beside = held === "mini" || held === "dock" || held === "bar";
  const frame: Frame =
    beside && (phone || onOwnPage) ? "closed" : held === "dock" && !canDock ? "mini" : held;
  /** The frame the bar was minimized from, which it opens again. */
  const barFrom = useRef<Frame>("mini");
  const canAsk = useCan("assistant:use");
  const startsNew = useRef(false);
  const wantsFocus = useRef(false);
  const [pointedCount, setPointedCount] = useState(0);
  /** The open right-click menu: where, what it is about, and the element that is ringed. */
  const [menu, setMenu] = useState<{
    at: { x: number; y: number };
    pointed: Pointed;
    element: Element;
  } | null>(null);
  const frameNow = useRef(frame);
  frameNow.current = frame;
  const stream = useConversationStream(frame !== "closed" || onOwnPage ? conversationId : null);
  // Which conversation was last read and found with nothing said. Kept while no frame is open
  // and the stream is not followed: nothing can be said in it meanwhile from here.
  const blankId = useRef<number | null>(null);
  if (conversationId !== null && stream.loaded) {
    blankId.current = isBlank(stream) ? conversationId : null;
  }
  const blank = conversationId !== null && blankId.current === conversationId;
  const now = useRef({ draft, onOwnPage, pathname, blank, base });
  now.current = { draft, onOwnPage, pathname, blank, base };
  const scrollTop = useRef<number | null>(null);
  // Null until a page is visited: a conversation opened straight onto /assistant is about none.
  const contextPage = useRef<string | null>(pathname === ASSISTANT_PATH ? null : pathname);
  /** The card being shown; `key` is new for every press, so a second press points afresh.
   *  `from` is the frame the chip was pressed in, which Back to chat returns to. */
  const [shown, setShown] = useState<{
    card: Shown;
    key: number;
    from: Frame;
  } | null>(null);
  const go = useRef(navigate);
  useEffect(() => {
    go.current = navigate;
  }, [navigate]);
  if (pathname !== ASSISTANT_PATH) contextPage.current = pathname;

  // biome-ignore lint/correctness/useExhaustiveDependencies: `setPrefs` is new each render and writes the same store
  const assistant = useMemo<Assistant>(
    () => ({
      open: () => {
        setShown(null);
        setHeld("closed");
        go.current(ASSISTANT_PATH);
      },
      openDrawer: () => {
        setShown(null);
        setHeld("drawer");
      },
      close: () => setHeld("closed"),
      showMe: (card) => {
        const from = frameNow.current;
        // The Drawer is over the page the card points into; a frame beside the page is not.
        if (from === "drawer") setHeld("closed");
        setShown((previous) => ({ card, key: (previous?.key ?? 0) + 1, from }));
        go.current(showMeHref(card));
      },
      showWhere: (pointed) => {
        if (typeof pointed.page !== "string" || !isDashboardPath(pointed.page)) return;
        assistantNow.current?.showMe({
          path: pointed.page,
          anchor: pointed.anchor ?? null,
          note: null,
          link: pointed.href ?? null,
        });
      },
      frame,
      minimize: () => {
        if (frameNow.current !== "bar" && frameNow.current !== "closed") {
          barFrom.current = frameNow.current;
        }
        setHeld("bar");
      },
      restore: () => setHeld(barFrom.current),
      float: () => setHeld("mini"),
      dock: () => setHeld("dock"),
      canDock,
      prefs,
      setPrefs,
      pointAt: (given, mode) => {
        const pointed = capped(given);
        if (mode === "new") {
          // A conversation with nothing said yet is the new one already.
          if (!now.current.blank) {
            startsNew.current = true;
            scrollTop.current = null;
            setConversationId(null);
          }
          // The new conversation starts with this one thing: what waited for the old one goes.
          setDraft((held) => ({ ...held, pointed: [pointed], files: [] }));
        } else if (now.current.draft.pointed.length >= MAX_POINTED) {
          showFlag({ appearance: "info", title: "A message can point at five things" });
          return;
        } else {
          setDraft((held) => ({ ...held, pointed: [...held.pointed, pointed] }));
        }
        wantsFocus.current = true;
        setPointedCount((count) => count + 1);
        setShown(null);
        // The assistant's own page is the conversation; a frame beside the page stays as it is,
        // unless a drawer or a dialog is over the page and so over the frame: then the Drawer
        // opens on top, where the person can see what they pointed at and write.
        const covered = document.getElementById("root")?.inert === true;
        if (frameNow.current === "drawer") return;
        if (covered) setHeld("drawer");
        else if (frameNow.current === "bar") setHeld(barFrom.current);
        else if (frameNow.current === "closed" && !now.current.onOwnPage) setHeld("drawer");
      },
      pointEntries: (pointed) =>
        canAsk
          ? {
              group: "Assistant",
              items: [
                {
                  label: "Start new conversation",
                  icon: "plus",
                  onClick: () => assistantNow.current?.pointAt(onThisPage(pointed()), "new"),
                },
                {
                  label: "Add to current conversation",
                  icon: "sparkles",
                  onClick: () => assistantNow.current?.pointAt(onThisPage(pointed()), "add"),
                },
              ],
            }
          : null,
    }),
    [frame, prefs, canDock, canAsk],
  );
  const assistantNow = useRef<Assistant | null>(null);
  assistantNow.current = assistant;
  /** A place with a menu of its own knows the thing, not the page it is on. */
  const onThisPage = (pointed: Pointed): Pointed => ({
    ...pointed,
    page: pointed.page ?? now.current.pathname,
  });

  // One listener for the whole dashboard. It runs after the page's own handlers, so a place with
  // a menu of its own has already said so.
  useEffect(() => {
    if (!canAsk || phone) return;
    // What was selected when the right button went down. Some systems select the word under the
    // pointer between that press and the menu: a selection that was not there at the press is
    // theirs, not the person's, and is put back as it was.
    let atPress: string | null = null;
    const onPress = (event: PointerEvent) => {
      if (event.button === 2) atPress = String(window.getSelection() ?? "");
    };
    const onMenu = (event: globalThis.MouseEvent) => {
      const pressed = atPress;
      atPress = null;
      if (event.defaultPrevented || keepsNativeMenu(event)) return;
      const target = event.target as Element;
      const element = pointedElement(target);
      event.preventDefault();
      const selection = window.getSelection();
      // The keyboard's menu key comes with no press: its selection is the person's.
      const theirs = pressed === null || String(selection ?? "") === pressed;
      if (!theirs && !(element instanceof HTMLTextAreaElement)) selection?.removeAllRanges();
      // The keyboard's menu key has no pointer: the menu opens at the thing itself.
      const box = element.getBoundingClientRect();
      const keyed = event.button !== 2 && event.clientX === 0 && event.clientY === 0;
      setMenu({
        at: keyed ? { x: box.left, y: box.bottom } : { x: event.clientX, y: event.clientY },
        pointed: describeTarget(target, now.current.pathname, theirs, now.current.base),
        element,
      });
    };
    document.addEventListener("pointerdown", onPress, true);
    document.addEventListener("contextmenu", onMenu);
    return () => {
      document.removeEventListener("pointerdown", onPress, true);
      document.removeEventListener("contextmenu", onMenu);
    };
  }, [canAsk, phone]);
  const ringed = menu?.element;
  useEffect(() => {
    if (!ringed || ringed.hasAttribute(POINTED_AT)) return;
    ringed.setAttribute(POINTED_AT, "");
    return () => ringed.removeAttribute(POINTED_AT);
  }, [ringed]);
  const pointing = menu ? assistant.pointEntries(() => menu.pointed) : null;
  const session = useMemo<AssistantSession>(
    () => ({
      conversationId,
      onConversation: setConversationId,
      scrollTop,
      contextPage,
      draft,
      setDraft,
      isWide,
      setWide,
      stream,
      startsNew,
      wantsFocus,
      pointedCount,
    }),
    [conversationId, draft, isWide, stream, pointedCount],
  );

  // A page the person went to themselves ends the pointing: the control is not on it.
  const shownPath = shown?.card.path;
  // biome-ignore lint/correctness/useExhaustiveDependencies: a new page is the event, not the card
  useEffect(() => {
    if (shownPath !== undefined && pathname !== shownPath) setShown(null);
  }, [pathname]);

  return (
    <AssistantContext.Provider value={assistant}>
      <SessionContext.Provider value={session}>
        {children}
        {(frame === "drawer" || frame === "mini") && (
          <Suspense fallback={null}>
            <AssistantChat variant={frame} session={session} onClose={assistant.close} />
          </Suspense>
        )}
        {frame === "bar" && <AssistantBar turn={stream.turn} onOpen={assistant.restore} />}
        <ContextMenu
          at={pointing ? (menu?.at ?? null) : null}
          label="Assistant"
          items={pointing ? [pointing] : []}
          onClose={() => setMenu(null)}
        />
        {shown && frame !== "drawer" && (
          <ShowMeSpotlight
            key={shown.key}
            card={shown.card}
            base={base}
            onBack={
              shown.from === "drawer"
                ? assistant.openDrawer
                : shown.from === "closed"
                  ? assistant.open
                  : undefined
            }
            onDismiss={() => setShown(null)}
          />
        )}
      </SessionContext.Provider>
    </AssistantContext.Provider>
  );
}

/** The docked conversation, where the shell puts it: in its row, after the page. */
export function AssistantDock() {
  const assistant = useAssistant();
  const session = useAssistantSession();
  if (assistant.frame !== "dock") return null;
  return (
    <Suspense fallback={null}>
      <AssistantChat variant="dock" session={session} onClose={assistant.close} />
    </Suspense>
  );
}

export { isDashboardPath };

/**
 * A link to a dashboard page from inside the conversation, followed by the router. In the Drawer,
 * which is modal and would stay over the page the link opens, following it closes the Drawer
 * first. A conversation docked or floating beside the page stays: the page the link opens is
 * what the person reads it beside. Never blue: the text's own colour, underlined.
 */
export function DashboardLink({ to, children }: { to: string; children: ReactNode }) {
  const { close, frame } = useAssistant();
  return (
    <Link
      to={to}
      onClick={(event: MouseEvent) => {
        if (frame === "drawer" && !event.metaKey && !event.ctrlKey && !event.shiftKey) close();
      }}
      className="font-medium underline underline-offset-2"
    >
      {children}
    </Link>
  );
}
