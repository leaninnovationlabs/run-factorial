/**
 * Show me, on the page it led to: a ring on the control the card names, its note in a callout
 * beside it, and a floating Back to chat.
 *
 * The page may still be arriving — its chunk loading, its data on the way — so the control is
 * watched for rather than looked up once, and never on the page fading out behind it
 * (`pageTransition.ts`). A control that has not appeared after `ANCHOR_WAIT_MS` is not waited on
 * any longer: the note shows in the floating chip, with no ring and nothing spinning. It is still
 * ringed if it turns up later.
 *
 * The ring is an attribute, `data-assist-shown` (`assistant.css`), not a class: React rewrites the
 * class of an element it renders, and would take the ring with it.
 */

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ShowMePayload } from "../../api/types";
import { Button, IconButton, prefersReducedMotion } from "../ui";
import { place } from "../ui/floating";
import { type AssistAnchor, anchorSelector, isAssistAnchor } from "./assistAnchors";

export const ANCHOR_WAIT_MS = 3000;

const SHOWN = "data-assist-shown";

export function showMeHref(card: ShowMePayload): string {
  return card.path;
}

function findAnchor(name: AssistAnchor): HTMLElement | null {
  for (const element of document.querySelectorAll<HTMLElement>(anchorSelector(name))) {
    if (!element.closest('[data-page-phase="out"]')) return element;
  }
  return null;
}

export function ShowMeSpotlight({
  card,
  onBack,
  onDismiss,
}: {
  card: ShowMePayload;
  onBack: () => void;
  onDismiss: () => void;
}) {
  const anchor = isAssistAnchor(card.anchor) ? card.anchor : null;
  const [target, setTarget] = useState<HTMLElement | null>(null);
  const [waited, setWaited] = useState(anchor === null);
  const note = card.note?.trim() || null;

  useEffect(() => {
    if (!anchor) return;
    let ringed: HTMLElement | null = null;
    let scrolled = false;
    const look = () => {
      const found = findAnchor(anchor);
      if (found === ringed) return;
      ringed?.removeAttribute(SHOWN);
      ringed = found;
      if (found) {
        found.setAttribute(SHOWN, "");
        if (!scrolled) {
          scrolled = true;
          found.scrollIntoView({
            block: "center",
            behavior: prefersReducedMotion() ? "auto" : "smooth",
          });
        }
      }
      setTarget(found);
    };
    look();
    // Once a frame at most: a page that polls changes the document many times a second, and each
    // look searches all of it.
    let frame = 0;
    const observer = new MutationObserver(() => {
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        look();
      });
    });
    observer.observe(document.body, {
      childList: true,
      subtree: true,
      attributeFilter: ["data-page-phase"],
    });
    const timer = setTimeout(() => setWaited(true), ANCHOR_WAIT_MS);
    return () => {
      observer.disconnect();
      cancelAnimationFrame(frame);
      clearTimeout(timer);
      ringed?.removeAttribute(SHOWN);
    };
  }, [anchor]);

  if (typeof document === "undefined") return null;
  return createPortal(
    <>
      {target && note && <Callout anchor={target} text={note} />}
      <div className="fixed right-4 bottom-[calc(1rem+var(--tab-bar-clearance,0px))] z-layer flex max-w-sm items-center gap-2 rounded-lg bg-surface-overlay p-2 shadow-overlay">
        {!target && waited && note && (
          <p role="note" className="min-w-0 px-1 text-body text-default">
            {note}
          </p>
        )}
        <Button appearance="primary" onClick={onBack}>
          Back to chat
        </Button>
        <IconButton icon="close" label="Stop showing" onClick={onDismiss} />
      </div>
    </>,
    document.body,
  );
}

/** The note beside the ringed control, kept beside it as the page scrolls. */
function Callout({ anchor, text }: { anchor: HTMLElement; text: string }) {
  const bubble = useRef<HTMLParagraphElement>(null);
  const [at, setAt] = useState<{ top: number; left: number } | null>(null);
  useLayoutEffect(() => {
    const measure = () => {
      if (!bubble.current) return;
      setAt(
        place(
          anchor.getBoundingClientRect(),
          bubble.current.getBoundingClientRect(),
          { width: window.innerWidth, height: window.innerHeight },
          "bottom",
          8,
        ),
      );
    };
    measure();
    // Capture: `<main>` scrolls, not the window, and a scroll event does not bubble.
    document.addEventListener("scroll", measure, true);
    window.addEventListener("resize", measure);
    return () => {
      document.removeEventListener("scroll", measure, true);
      window.removeEventListener("resize", measure);
    };
  }, [anchor]);
  return (
    <p
      ref={bubble}
      role="note"
      style={at ? { top: at.top, left: at.left } : { top: -9999, left: -9999 }}
      className="fixed z-layer max-w-72 rounded-md bg-neutral-bold px-3 py-2 text-body text-inverse shadow-overlay"
    >
      {text}
    </p>
  );
}
