/**
 * The bar along a phone's foot: the dashboard's two primary tasks on a phone — tickets and the
 * assistant — under the thumb, and Menu, which opens the side navigation over the page for
 * everything else (docs/design-system.md, "Phone width"). Below `md` only, where the side
 * navigation has left the page; the top bar then carries no menu button of its own.
 *
 * Tickets is the project's list, and only inside a project, a ticket's page included: tickets
 * belong to a project, and at the organization's level the way to them is choosing one. A
 * ticket's page has the tab once its project is known.
 * The Assistant follows the side navigation: inside a project it opens the Drawer over the page,
 * so the question is about what is behind it; outside, it is the assistant's own page.
 */

import { Link, useLocation } from "react-router-dom";
import { useCan } from "../../api/organizationQueries";
import { ASSISTANT_PATH, useAssistant } from "../assistant/AssistantProvider";
import { type ProjectPlace, projectTicketsHref, useProjectPlace } from "../project";
import { cx, type Glyph, Icon, Pressable } from "../ui";
import { SIDE_NAV_ID } from "./SideNav";

export type TabTargets = {
  /** Null outside a project. */
  tickets: { to: string; isCurrent: boolean } | null;
  /** Null for a reader who may not use the assistant. */
  assistant: { kind: "drawer" } | { kind: "page"; isCurrent: boolean } | null;
};

/** Where each tab goes from `pathname`, and which one names the page. */
export function tabTargets(
  place: ProjectPlace | null,
  pathname: string,
  canAsk: boolean,
): TabTargets {
  const onTicket = pathname.startsWith("/tickets/");
  const tickets = place
    ? { to: projectTicketsHref(place.projectId), isCurrent: place.area === "tickets" || onTicket }
    : null;
  if (!canAsk) return { tickets, assistant: null };
  return {
    tickets,
    assistant: place
      ? { kind: "drawer" }
      : { kind: "page", isCurrent: pathname === ASSISTANT_PATH },
  };
}

/** A tab's layout. The two that are buttons take their states from `Pressable`'s subtle face;
 *  the links match it. */
const TAB =
  "focus-inset relative flex min-h-14 flex-1 flex-col items-center justify-center gap-0.5 text-body-sm";
const linkLook = (isCurrent: boolean) =>
  cx(
    TAB,
    "transition-colors hover:bg-neutral-subtle-hovered active:bg-neutral-subtle-pressed",
    isCurrent ? "font-medium text-selected" : "text-default",
  );

function TabFace({ icon, label, isCurrent }: { icon: Glyph; label: string; isCurrent: boolean }) {
  return (
    <>
      {/* "You are here" in shape as well as colour, as the side navigation's rows say it. */}
      <span
        aria-hidden="true"
        className={cx(
          "absolute inset-x-6 top-0 h-0.5 rounded-full",
          isCurrent && "bg-neutral-bold",
        )}
      />
      <Icon glyph={icon} />
      <span>{label}</span>
    </>
  );
}

export function TabBar({ menuOpen, onMenu }: { menuOpen: boolean; onMenu: () => void }) {
  const { pathname } = useLocation();
  const place = useProjectPlace();
  const canAsk = useCan("assistant:use");
  const assistant = useAssistant();
  const targets = tabTargets(place, pathname, canAsk);
  return (
    <nav
      aria-label="Main tasks"
      data-tab-bar
      // The home indicator's inset, below the row, so a tab is never under it.
      className="flex shrink-0 border-t border-default bg-surface pb-[env(safe-area-inset-bottom)]"
    >
      {targets.tickets && (
        <Link
          to={targets.tickets.to}
          aria-current={targets.tickets.isCurrent ? "page" : undefined}
          className={linkLook(targets.tickets.isCurrent)}
        >
          <TabFace icon="tickets" label="Tickets" isCurrent={targets.tickets.isCurrent} />
        </Link>
      )}
      {targets.assistant?.kind === "page" && (
        <Link
          to={ASSISTANT_PATH}
          aria-current={targets.assistant.isCurrent ? "page" : undefined}
          className={linkLook(targets.assistant.isCurrent)}
        >
          <TabFace icon="sparkles" label="Assistant" isCurrent={targets.assistant.isCurrent} />
        </Link>
      )}
      {targets.assistant?.kind === "drawer" && (
        <Pressable
          appearance="subtle"
          aria-haspopup="dialog"
          onClick={assistant.openDrawer}
          className={TAB}
        >
          <TabFace icon="sparkles" label="Assistant" isCurrent={false} />
        </Pressable>
      )}
      {/* Open, the navigation covers this bar, and its own Close is the way back. */}
      <Pressable
        appearance="subtle"
        aria-expanded={menuOpen}
        aria-controls={SIDE_NAV_ID}
        onClick={onMenu}
        className={TAB}
      >
        <TabFace icon="menu" label="Menu" isCurrent={false} />
      </Pressable>
    </nav>
  );
}
