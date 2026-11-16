/**
 * The project the page is in, after its organization in the top bar, and the way to another. Its
 * name is a link to the project's dashboard; the chevron beside it opens a menu of the
 * organization's active projects, the current one checked, that opens the same page of
 * the one chosen — the tickets of another project from this one's tickets. A paused project is
 * reached from the Projects list, which the menu ends with; it would otherwise list every toy ever
 * made. The navigation drawn over the page covers the top bar, so it carries this too, on a band
 * of its own (`SideNav`).
 */

import { useRef } from "react";
import { useNavigate } from "react-router-dom";
import { useProject, useProjects } from "../../api/queries";
import type { MenuEntry } from "../ui";
import { DropdownMenu, NavPlace, NavPlaceLink, NavPlaceMenuButton, Skeleton } from "../ui";
import { type ProjectPlace, projectHref, sameAreaHref } from "./place";

export function ProjectSwitcher({
  place,
  onNavigate = () => {},
}: {
  place: ProjectPlace;
  /** After a choice: the overlay navigation closes itself. */
  onNavigate?: () => void;
}) {
  const navigate = useNavigate();
  const anchor = useRef<HTMLDivElement | null>(null);
  const { projectId, area } = place;
  const projectName = useProject(projectId).data?.name;
  const projects = useProjects({ live: false });
  const active = (projects.data ?? [])
    .filter((project) => project.active)
    .sort((a, b) => a.name.localeCompare(b.name));
  const go = (to: string) => {
    onNavigate();
    navigate(to);
  };
  const items: MenuEntry[] = [
    ...(active.length > 0
      ? [
          {
            group: "Active projects",
            items: active.map((project) => ({
              label: project.name,
              description: project.github_repo,
              isSelected: project.id === projectId,
              onClick: () => {
                if (project.id !== projectId) go(sameAreaHref(project.id, area));
              },
            })),
          },
        ]
      : []),
    {
      label: "All projects",
      description: "Every project, paused ones too",
      onClick: () => go("/projects?show=all"),
    },
  ];
  const label = projectName ? `Switch project: ${projectName}` : "Switch project";
  return (
    <DropdownMenu
      placement="bottom-start"
      anchorRef={anchor}
      items={items}
      trigger={{
        label,
        render: (props, { isOpen }) => (
          <NavPlace ref={anchor} isOpen={isOpen}>
            <NavPlaceLink
              data-project-home
              to={projectHref(projectId)}
              icon="projects"
              name={projectName}
              pending={<Skeleton width="6rem" height="0.875rem" />}
              onClick={onNavigate}
            />
            <NavPlaceMenuButton {...props} data-project-switcher label={label} isOpen={isOpen} />
          </NavPlace>
        ),
      }}
    />
  );
}
