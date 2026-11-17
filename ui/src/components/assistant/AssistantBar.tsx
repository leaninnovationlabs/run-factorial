/**
 * The assistant minimized: a small bar at the page's bottom right corner that opens the
 * conversation again where it was, in the frame it was put away from. It is drawn inside the
 * app, not on the document's body, so a drawer or a dialog makes it inert with the rest of the
 * page. It says the two things a person who put the conversation away still wants to know: that
 * a reply is being written, and that one has arrived since.
 */

import { useEffect, useRef, useState } from "react";
import logoUrl from "../../../logo.svg";
import type { ChatTurn } from "../../api/types";
import { Lozenge, Pressable, VisuallyHidden, WorkingDots } from "../ui";
import { isLive } from "./useConversationStream";

export function AssistantBar({ turn, onOpen }: { turn: ChatTurn | null; onOpen: () => void }) {
  const live = isLive(turn);
  const wasLive = useRef(live);
  const [arrived, setArrived] = useState(false);
  useEffect(() => {
    if (wasLive.current && !live) setArrived(true);
    wasLive.current = live;
  }, [live]);

  return (
    <div className="fixed right-4 bottom-[calc(1rem+var(--tab-bar-clearance,0px))] z-layer overflow-hidden rounded-lg bg-surface-overlay shadow-overlay">
      <Pressable
        data-assistant-bar
        appearance="subtle"
        onClick={onOpen}
        className="flex h-10 items-center gap-2 px-3"
      >
        <img src={logoUrl} alt="" className="size-5 shrink-0 rounded-xs" />
        <span className="text-body font-semibold">
          <VisuallyHidden>Open the </VisuallyHidden>Assistant
        </span>
        {live && <WorkingDots />}
        {live && <VisuallyHidden>, a reply is being written</VisuallyHidden>}
        {!live && arrived && <Lozenge appearance="information">New reply</Lozenge>}
      </Pressable>
    </div>
  );
}
