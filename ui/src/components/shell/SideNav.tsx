/**
 * The side navigation: the pages of the place the top bar names, and at its foot where to read
 * about them and, last, the account: who is signed in, the theme as a row of icons, and the way
 * to sign out. Which organization and which project the reader is in, and the way to another, are
 * the top bar's (`TopNav`).
 *
 * Outside a project it names the organization's places — Overview, Projects, the Assistant — and
 * a Settings group under them: Runtimes, MCP access and the assistant's settings, each shown only
 * to a reader who holds the permission its page needs, and the group not at all when none is.
 * Inside one — a project's own pages, or a ticket's, which belongs to a project — it is the
 * project's: its pages with its configuration under them (`ProjectNav`).
 *
 * Drawn over the page it covers the top bar, so it carries what the bar says: the product, the
 * organization's switcher, and inside a project the project's, on a filled band from edge to edge.
 * The band sits above the region the items scroll in, which would otherwise clip it to the
 * navigation's padding.
 *
 * Full or compact on a wide screen, as the reader chose; compact whatever the choice below
 * 1024px, where each item is an icon named by its label and a tooltip; and below 768px off the
 * page altogether, opening over it from the tab bar's Menu (`AppShell` decides which).
 */

import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { useLocation } from "react-router-dom";
import logoUrl from "../../../logo.svg";
import { useOrganizationDetail } from "../../api/organizationQueries";
import { useVersion } from "../../api/versionQueries";
import { DOCS } from "../../docs";
import { usePersistentState } from "../../storage";
import { type ThemeChoice, useTheme } from "../../theme";
import { ASSISTANT_PATH } from "../assistant/AssistantProvider";
import { ProjectNav, ProjectSwitcher, useProjectPlace } from "../project";
import {
  cx,
  DropdownMenu,
  type Glyph,
  Icon,
  IconButton,
  type MenuEntry,
  NavButtonItem,
  NavLinkItem,
} from "../ui";
import { pushLayer } from "../ui/layers";
import { OrganizationSwitcher } from "./OrganizationSwitcher";
import { preloadRoute } from "./routes";

export const SIDE_NAV_ID = "side-nav";

export type SideNavMode = "full" | "compact" | "overlay" | "hidden";

/** `needs` is the permission the page's reads need: an item the API would refuse is not shown. */
type Item = { to: string; icon: Glyph; label: string; match?: string[]; needs?: string };

const PLACES: Item[] = [
  { to: "/overview", icon: "overview", label: "Overview" },
  // A ticket belongs to a project, so reading one keeps Projects lit — for the moment before the
  // ticket lands and the navigation becomes its project's.
  { to: "/projects", icon: "projects", label: "Projects", match: ["/projects", "/tickets"] },
  { to: ASSISTANT_PATH, icon: "sparkles", label: "Assistant", needs: "assistant:use" },
];

/** What the organization sets up once and then works with: a group of its own that folds away. */
const SETTINGS: Item[] = [
  { to: "/runtimes", icon: "runtimes", label: "Runtimes", needs: "runtimes:manage" },
  { to: "/account/mcp", icon: "key", label: "MCP access", needs: "mcp_tokens:mint" },
  { to: "/settings", icon: "sparkles", label: "Assistant settings", needs: "assistant:configure" },
];

/** Who may act in the organization (design.md §7.19). */
const MEMBERS: Item = { to: "/members", icon: "users", label: "Members", needs: "members:invite" };

/** The theme, as the account menu's row of icons: one press, never a menu of its own. */
const THEME_CHOICES: { value: ThemeChoice; label: string; glyph: Glyph }[] = [
  { value: "light", label: "Light", glyph: "sun" },
  { value: "dark", label: "Dark", glyph: "moon" },
  { value: "system", label: "Match system theme", glyph: "monitor" },
];

export const SETTINGS_OPEN_KEY = "sf-ui:nav-settings-open";
const SETTINGS_GROUP_ID = "nav-settings-pages";
// Module-level, so the setter `usePersistentState` returns keeps its identity across renders.
const parseFlag = (stored: string | null) => stored === "1";
const serializeFlag = (on: boolean) => (on ? "1" : "0");

/** Whether an item names the page at `pathname`: its own path, or any path under one it claims.
 *  `/projects-archive` is not under `/projects`. */
export function itemActive(item: Item, pathname: string): boolean {
  return (item.match ?? [item.to]).some(
    (prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`),
  );
}

export function SideNav({
  mode,
  onClose,
  sessionEmail = null,
  onSignOut = () => {},
}: {
  mode: SideNavMode;
  onClose: () => void;
  sessionEmail?: string | null;
  onSignOut?: () => void;
}) {
  const { pathname } = useLocation();
  const panel = useRef<HTMLElement>(null);
  const place = useProjectPlace();
  const { choice, setChoice } = useTheme();
  const revision = useVersion().data?.revision;

  // Over the page it is modal, as a menu is: the page behind goes inert, Escape and the blanket
  // close it, and following a link closes it. Portalled for that reason — the layer stack makes
  // `#root` inert, and a navigation drawn inside it would go inert with the page.
  useEffect(() => {
    if (mode !== "overlay" || !panel.current) return;
    const pop = pushLayer(panel.current, onClose);
    // The first of the place's pages, past the organization's and the project's names, which
    // are links too.
    panel.current.querySelector<HTMLElement>("[data-nav-items] a")?.focus();
    return pop;
  }, [mode, onClose]);

  if (mode === "hidden") return null;
  const compact = mode === "compact";
  const open = (url: string) => window.open(url, "_blank", "noopener,noreferrer");
  const nav = (
    <nav
      ref={panel}
      id={SIDE_NAV_ID}
      aria-label="Primary"
      className={cx(
        "flex shrink-0 flex-col gap-1 border-r border-default bg-surface p-2",
        mode === "overlay" ? "fixed inset-y-0 left-0 z-modal w-64 shadow-overlay" : "h-full",
        compact ? "w-14 items-center" : mode === "full" && "w-56",
      )}
    >
      {mode === "overlay" && (
        <>
          <div className="mb-2 flex items-center justify-between gap-2 pl-2">
            <span className="flex items-center gap-2">
              <img data-nav-mark src={logoUrl} alt="" className="size-6" />
              <span className="text-heading-xs text-default">Factorial</span>
            </span>
            <IconButton icon="close" label="Close navigation" onClick={onClose} />
          </div>
          <OrganizationSwitcher onNavigate={onClose} />
          {place && (
            <div
              data-project-band
              // Out to the navigation's edges, past its own padding: the band is the place, not a
              // row in it.
              className="-mx-2 my-1 self-stretch border-y border-default bg-neutral p-2"
            >
              <ProjectSwitcher place={place} onNavigate={onClose} />
            </div>
          )}
        </>
      )}
      {/* The items scroll on their own when a project's configuration outgrows a short window,
          so the foot stays where it is. */}
      <div
        data-nav-items
        className={cx(
          "flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto",
          compact && "items-center",
        )}
      >
        {place ? (
          <ProjectNav
            place={place}
            isCompact={compact}
            onNavigate={mode === "overlay" ? onClose : undefined}
          />
        ) : (
          <OrganizationNav
            pathname={pathname}
            isCompact={compact}
            onNavigate={mode === "overlay" ? onClose : undefined}
          />
        )}
      </div>
      <div
        className={cx(
          "mt-auto flex flex-col gap-1 border-t border-default pt-2",
          compact ? "items-center" : "w-full",
        )}
      >
        <FootMenu
          icon="help"
          label="Help"
          isCompact={compact}
          heading={revision ? `Build ${revision.slice(0, 7)}` : undefined}
          items={[
            {
              label: "Process configuration reference",
              description: "Every field of a process file",
              onClick: () => open(`${DOCS}/processes/`),
            },
            {
              label: "Point Factorial at a repository",
              description: "The .factorial/ folder, from scratch",
              onClick: () => open(`${DOCS}/connect-a-repository/`),
            },
          ]}
        />
        <FootMenu
          icon="user"
          label="Account"
          isCompact={compact}
          heading={sessionEmail ? `Signed in as ${sessionEmail}` : undefined}
          items={[
            {
              label: "Theme",
              choices: THEME_CHOICES.map((theme) => ({
                label: theme.label,
                glyph: theme.glyph,
                isSelected: choice === theme.value,
                onClick: () => setChoice(theme.value),
              })),
            },
            { label: "Sign out", onClick: onSignOut },
          ]}
        />
      </div>
    </nav>
  );
  if (mode !== "overlay") return nav;
  return createPortal(
    <>
      <div aria-hidden="true" className="fixed inset-0 z-blanket bg-blanket" onClick={onClose} />
      {nav}
    </>,
    document.body,
  );
}

/**
 * The organization's own places — Overview, Projects, the Assistant — then Settings, a group that
 * folds away: Runtimes, MCP access and the assistant's settings. The fold is the reader's, kept in
 * the browser, and going to one of its pages unfolds it. Each page keeps its icon inside the group,
 * as a project's configuration pages do. Compact, there is no group to fold: each page is an icon
 * of its own, after a rule.
 */
function OrganizationNav({
  pathname,
  isCompact,
  onNavigate,
}: {
  pathname: string;
  isCompact: boolean;
  onNavigate?: () => void;
}) {
  const [open, setOpen] = usePersistentState(SETTINGS_OPEN_KEY, parseFlag, serializeFlag);
  const held = useOrganizationDetail().data?.permissions ?? [];
  const reachable = (item: Item) => item.needs === undefined || held.includes(item.needs);
  const places = PLACES.filter(reachable);
  const settings = [...SETTINGS, MEMBERS].filter(reachable);
  const onSettings = settings.some((item) => itemActive(item, pathname));
  useEffect(() => {
    if (onSettings) setOpen(true);
  }, [onSettings, setOpen]);
  const row = (item: Item, isChild = false) => (
    <NavLinkItem
      key={item.to}
      to={item.to}
      icon={item.icon}
      label={item.label}
      isCompact={isCompact}
      isChild={isChild}
      isCurrent={itemActive(item, pathname)}
      onClick={onNavigate}
      onMouseEnter={() => preloadRoute(item.to)}
      onFocus={() => preloadRoute(item.to)}
    />
  );
  return (
    <>
      {places.map((item) => row(item))}
      {settings.length === 0 ? null : isCompact ? (
        <>
          <span aria-hidden="true" className="my-1 w-6 border-t border-default" />
          {settings.map((item) => row(item))}
        </>
      ) : (
        <>
          <NavButtonItem
            icon="sliders"
            label="Settings"
            aria-expanded={open}
            aria-controls={SETTINGS_GROUP_ID}
            onClick={() => setOpen((previous) => !previous)}
            trailing={
              <Icon glyph={open ? "chevron-down" : "chevron-right"} className="text-icon-subtle" />
            }
          />
          <div id={SETTINGS_GROUP_ID} hidden={!open} className="flex flex-col gap-1">
            {settings.map((item) => row(item, true))}
          </div>
        </>
      )}
    </>
  );
}

/** A menu at the navigation's foot, opening upward from the row that names it. */
function FootMenu({
  icon,
  label,
  isCompact,
  heading,
  items,
}: {
  icon: Glyph;
  label: string;
  isCompact: boolean;
  heading?: string;
  items: MenuEntry[];
}) {
  return (
    <DropdownMenu
      placement="top-start"
      heading={heading}
      items={items}
      trigger={{
        label,
        render: (props, { isOpen }) => (
          <NavButtonItem
            {...props}
            icon={icon}
            label={label}
            isCompact={isCompact}
            isOpen={isOpen}
          />
        ),
      }}
    />
  );
}
