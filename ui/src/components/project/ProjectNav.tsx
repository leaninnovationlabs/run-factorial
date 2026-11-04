/**
 * The side navigation's items inside a project: Dashboard, Tickets, the Assistant — which opens
 * over the page the reader is on, as a Drawer, so the question is about what is behind it — and
 * Settings, a group of one row per configuration page that folds away. The project
 * itself — its name on a band across the navigation, the button that switches to another
 * (`ProjectSwitcher`) — heads the navigation above these, outside the region they scroll in
 * (`SideNav`).
 *
 * The Assistant shows only to a reader who may use it, and Settings only to one who holds
 * `projects:read`, which the configuration pages' reads need.
 *
 * A configuration row carries its readiness where that needs a look: a glyph for a row that
 * blocks activation or needs attention, and on Settings itself the count that block.
 * Folded, Settings also carries a warning a hidden row would have shown, and it is the
 * current row when the page is one of those hidden under it.
 *
 * The group's fold is the reader's, kept in the browser. Going into the project's settings from
 * elsewhere unfolds it: a reader who went there wants its pages in view.
 *
 * It reads what the pages already read. The project's own payload names the core rows; the
 * repo-side probes are drawn only if a page cached them (`useProjectReadiness` disabled), because
 * a navigation mounted on every page of the project must never be what runs live GitHub calls.
 */

import { useEffect } from "react";
import { useCan } from "../../api/organizationQueries";
import { useProject, useProjectReadiness } from "../../api/queries";
import { usePersistentState } from "../../storage";
import { useAssistant } from "../assistant/AssistantProvider";
import { READINESS, readinessOf, sectionRows, UNCHECKED } from "../configuration/readiness";
import { preloadRoute } from "../shell/routes";
import { Badge, Icon, NavButtonItem, NavLinkItem } from "../ui";
import { configurationHref, type ProjectPlace, projectHref, projectTicketsHref } from "./place";

export const CONFIGURATION_OPEN_KEY = "sf-ui:nav-configuration-open";
const CONFIGURATION_GROUP_ID = "nav-configuration-pages";
// Module-level, so the setter `usePersistentState` returns keeps its identity: the effect that
// unfolds the group depends on it, and a new one each render would unfold it on every render.
const parseFlag = (stored: string | null) => stored === "1";
const serializeFlag = (on: boolean) => (on ? "1" : "0");

export function ProjectNav({
  place,
  isCompact,
  onNavigate,
}: {
  place: ProjectPlace;
  isCompact: boolean;
  /** Following a link closes the overlay navigation; undefined where it is not an overlay. */
  onNavigate?: () => void;
}) {
  const { projectId, area, section } = place;
  const canConfigure = useCan("projects:read");
  const canAsk = useCan("assistant:use");
  const project = useProject(projectId).data;
  const probes = useProjectReadiness(projectId, { enabled: false }).data;
  const sections = project ? sectionRows(project, probes, UNCHECKED) : [];
  const blocked = sections.filter(({ row }) => row?.state === "blocked").length;
  const warned = sections.some(({ row }) => row?.state === "warn");
  const onConfiguration = area === "configuration";
  const assistant = useAssistant();
  const [open, setOpen] = usePersistentState(CONFIGURATION_OPEN_KEY, parseFlag, serializeFlag);
  useEffect(() => {
    if (onConfiguration) setOpen(true);
  }, [onConfiguration, setOpen]);
  const preload = (to: string) => ({
    onMouseEnter: () => preloadRoute(to),
    onFocus: () => preloadRoute(to),
  });
  const shown = open && !isCompact;
  return (
    <div data-assist-anchor="project.nav" className="flex flex-col gap-1">
      <NavLinkItem
        to={projectHref(projectId)}
        icon="dashboard"
        label="Dashboard"
        isCompact={isCompact}
        isCurrent={area === "dashboard"}
        onClick={onNavigate}
        {...preload(projectHref(projectId))}
      />
      <NavLinkItem
        to={projectTicketsHref(projectId)}
        icon="tickets"
        label="Tickets"
        isCompact={isCompact}
        isCurrent={area === "tickets"}
        onClick={onNavigate}
        {...preload(projectTicketsHref(projectId))}
      />
      {canAsk && (
        <NavButtonItem
          icon="sparkles"
          label="Assistant"
          aria-haspopup="dialog"
          isCompact={isCompact}
          onClick={() => {
            onNavigate?.();
            assistant.openDrawer();
          }}
        />
      )}
      {canConfigure && (
        <>
          <NavLinkItem
            to={configurationHref(projectId)}
            icon="sliders"
            label="Settings"
            isCompact={isCompact}
            // Its pages hidden — compact, or folded — it stands for every one of them.
            isCurrent={onConfiguration && (!shown || section === null)}
            trailing={
              blocked > 0 ? (
                <Badge appearance="danger">{blocked}</Badge>
              ) : !shown && warned ? (
                <Icon
                  glyph={READINESS.warn.glyph}
                  label={READINESS.warn.label}
                  className={READINESS.warn.ink}
                />
              ) : undefined
            }
            expand={{
              isOpen: open,
              onToggle: () => setOpen((previous) => !previous),
              controls: CONFIGURATION_GROUP_ID,
            }}
            onClick={onNavigate}
            {...preload(configurationHref(projectId))}
          />
          <div id={CONFIGURATION_GROUP_ID} hidden={!shown} className="flex flex-col gap-1">
            {sections.map(({ section: entry, row }) => {
              const readiness =
                row && (row.state === "blocked" || row.state === "warn")
                  ? readinessOf(row.key, row.state)
                  : null;
              return (
                <NavLinkItem
                  key={entry.slug}
                  isChild
                  to={configurationHref(projectId, entry.slug)}
                  icon={entry.icon}
                  label={entry.title}
                  isCompact={isCompact}
                  isCurrent={onConfiguration && section === entry.slug}
                  trailing={
                    readiness && (
                      <Icon
                        glyph={readiness.glyph}
                        label={readiness.label}
                        className={readiness.ink}
                      />
                    )
                  }
                  onClick={onNavigate}
                  {...preload(configurationHref(projectId, entry.slug))}
                />
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
