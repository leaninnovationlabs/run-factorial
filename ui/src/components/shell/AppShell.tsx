/**
 * The frame every signed-in page is drawn in, after ADS's navigation system: a skip link, the top
 * bar, the connection banner, the side navigation, the page in `<main>`, and the flags. On a phone
 * the side navigation leaves the page and the tab bar (`TabBar`) takes the window's foot.
 *
 * The window never scrolls; `<main>` does. That is what gives a `fill` route (`routes.tsx`) a
 * height to fill, so a board, a log or a column can end exactly at the bottom of the window with
 * no script measuring where that is — and what keeps the top bar and the navigation in place.
 */

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { usePageTransition } from "../../pageTransition";
import { usePersistentState } from "../../storage";
import { AssistantProvider } from "../assistant/AssistantProvider";
import { cx, ErrorBoundary, FlagGroup, Spinner } from "../ui";
import { BELOW_LG, PHONE, useMedia } from "../ui/media";
import { ConnectionBanner } from "./ConnectionBanner";
import { FocusModeContext } from "./focusMode";
import { RequirePermission } from "./NoAccess";
import { NotFound } from "./NotFound";
import { ROUTES, routeFor, WIDTH_CLASS } from "./routes";
import { SideNav, type SideNavMode } from "./SideNav";
import { TabBar } from "./TabBar";
import { TopNav } from "./TopNav";

const COMPACT_STORAGE_KEY = "sf-ui:nav-compact";
const serializeFlag = (on: boolean) => (on ? "1" : "0");

/**
 * Which of the navigation's four forms to draw, and what its menu button says it does. On a wide
 * screen the top bar's button chooses full or compact, and the choice is kept; narrower, the
 * button opens the full navigation over the page instead — the tab bar's Menu, on a phone.
 */
export function navLayout(
  narrow: boolean,
  medium: boolean,
  compactChosen: boolean,
  overlayOpen: boolean,
): { mode: SideNavMode; label: string; expanded: boolean } {
  if (narrow || medium) {
    return {
      mode: overlayOpen ? "overlay" : narrow ? "hidden" : "compact",
      label: overlayOpen ? "Close navigation" : "Open navigation",
      expanded: overlayOpen,
    };
  }
  return {
    mode: compactChosen ? "compact" : "full",
    label: compactChosen ? "Expand navigation" : "Collapse navigation",
    expanded: !compactChosen,
  };
}

/**
 * The focus to the page's title in `main`. A page still arriving — its chunk loading — has no
 * title yet: `main` holds the focus meanwhile and hands it on when the title renders, unless the
 * reader has moved it by then. Returns what stops the wait.
 */
export function focusPageTitle(main: HTMLElement): () => void {
  const title = () => main.querySelector<HTMLElement>("[data-page-title]");
  const found = title();
  if (found) {
    found.focus({ preventScroll: true });
    return () => {};
  }
  main.focus({ preventScroll: true });
  const observer = new MutationObserver(() => {
    const arrived = title();
    if (!arrived) return;
    observer.disconnect();
    if (document.activeElement === main) arrived.focus({ preventScroll: true });
  });
  observer.observe(main, { childList: true, subtree: true });
  return () => observer.disconnect();
}

/**
 * After the page changes, the focus goes to its title, so a keyboard or screen-reader user starts
 * reading at the new page rather than at the link they pressed. Not on the first load, where the
 * browser's own start is right.
 */
function useFocusOnNavigate(pathname: string, phase: "in" | "out") {
  const first = useRef(true);
  // biome-ignore lint/correctness/useExhaustiveDependencies: a new page is the event, not a value read
  useEffect(() => {
    if (phase !== "in") return;
    if (first.current) {
      first.current = false;
      return;
    }
    let stop = () => {};
    const frame = requestAnimationFrame(() => {
      const main = document.getElementById("main");
      if (main) stop = focusPageTitle(main);
    });
    return () => {
      cancelAnimationFrame(frame);
      stop();
    };
  }, [pathname, phase]);
}

export function AppShell({
  sessionEmail,
  onSignOut,
}: {
  sessionEmail: string | null;
  onSignOut: () => void;
}) {
  // The location the shell paints, which trails the router's own while a page is on its way out.
  const { location: page, phase } = usePageTransition();
  const route = routeFor(page.pathname);
  const narrow = useMedia(PHONE);
  const medium = useMedia(BELOW_LG);
  const [compact, setCompact] = usePersistentState(
    COMPACT_STORAGE_KEY,
    (stored) => stored === "1",
    serializeFlag,
  );
  const [overlayOpen, setOverlayOpen] = useState(false);
  /** A page holding all of `main` (`useFocusMode`): the side navigation steps away. */
  const [focused, setFocused] = useState(false);
  const nav = navLayout(narrow, medium, compact, overlayOpen);
  const closeOverlay = useCallback(() => setOverlayOpen(false), []);
  useFocusOnNavigate(page.pathname, phase);
  // An overlay opened on a narrow window is not left open to reappear after the window grows.
  useEffect(() => {
    if (!narrow && !medium) setOverlayOpen(false);
  }, [narrow, medium]);

  const toggleNav = () => {
    if (narrow || medium) return setOverlayOpen((open) => !open);
    setCompact((previous) => !previous);
  };

  return (
    <AssistantProvider>
      <FocusModeContext.Provider value={setFocused}>
        <div
          data-app-shell
          className="flex h-dvh flex-col overflow-hidden bg-surface-sunken text-default"
        >
          <a
            href="#main"
            className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-tooltip focus:rounded-md focus:bg-surface-overlay focus:px-3 focus:py-2 focus:text-body focus:shadow-overlay"
          >
            Skip to main content
          </a>
          <TopNav
            navLabel={nav.label}
            navExpanded={nav.expanded}
            onToggleNav={focused || narrow ? undefined : toggleNav}
          />
          <ConnectionBanner />
          <div className="flex min-h-0 flex-1">
            {!focused && (
              <SideNav
                mode={nav.mode}
                onClose={closeOverlay}
                sessionEmail={sessionEmail}
                onSignOut={onSignOut}
              />
            )}
            {/* `relative`, so what the page positions — a visually hidden caption below the fold —
            is placed in the region that scrolls rather than stretching the document. */}
            <main id="main" tabIndex={-1} className="relative min-w-0 flex-1 overflow-y-auto">
              {/* The width follows the page being painted rather than the URL, so the column does not
              resize under a page that is still fading out. */}
              <div
                className={cx(
                  "mx-auto px-3 py-4 md:px-4 md:py-6 lg:px-8",
                  WIDTH_CLASS[route?.width ?? "default"],
                  route?.fill && "h-full",
                  route?.bleed && "max-md:p-0",
                )}
              >
                {/* `key` is what plays the entrance: a new pathname mounts a new element, and the
                animation runs on mount. It also resets the error boundary, so a page that failed
                to render takes nothing with it when the reader goes elsewhere. */}
                <div
                  key={page.pathname}
                  data-page-phase={phase}
                  className={cx("page-swap", route?.fill && "h-full")}
                >
                  <ErrorBoundary>
                    {/* A page's own chunk is loading: the spinner waits its own beat before it shows,
                    so a cached chunk never flashes it. */}
                    <Suspense fallback={<Spinner label="Loading the page" />}>
                      <Routes location={page}>
                        <Route path="/" element={<Navigate to="/overview" replace />} />
                        {ROUTES.map(({ path, page: { Component }, needs }) => (
                          <Route
                            key={path}
                            path={path}
                            element={
                              needs ? (
                                <RequirePermission permission={needs}>
                                  <Component />
                                </RequirePermission>
                              ) : (
                                <Component />
                              )
                            }
                          />
                        ))}
                        <Route path="*" element={<NotFound />} />
                      </Routes>
                    </Suspense>
                  </ErrorBoundary>
                </div>
              </div>
            </main>
          </div>
          {narrow && !focused && <TabBar menuOpen={overlayOpen} onMenu={toggleNav} />}
          <FlagGroup />
        </div>
      </FocusModeContext.Provider>
    </AssistantProvider>
  );
}
