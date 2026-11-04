/**
 * The project the page is in, at the head of its navigation on a band of its own so it reads as
 * the place rather than a page of it, and the way to another: a menu of the organization's active
 * projects, the current one checked, that opens the same page of the one chosen — the tickets of
 * another project from this one's tickets. A paused project is reached from the Projects list,
 * which the menu ends with; it would otherwise list every toy ever made.
 */

import { useNavigate } from "react-router-dom";
import { useProject, useProjects } from "../../api/queries";
import type { MenuEntry } from "../ui";
import { DropdownMenu, Icon, NavButtonItem, Skeleton } from "../ui";
import { type ProjectPlace, sameAreaHref } from "./place";

export function ProjectSwitcher({
  place,
  isCompact,
  onNavigate = () => {},
}: {
  place: ProjectPlace;
  isCompact: boolean;
  /** After a choice: the overlay navigation closes itself. */
  onNavigate?: () => void;
}) {
  const navigate = useNavigate();
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
      onClick: () => go("/projects"),
    },
  ];
  const label = projectName ? `Switch project: ${projectName}` : "Switch project";
  return (
    <DropdownMenu
      placement="bottom-start"
      items={items}
      trigger={{
        label,
        render: (props, { isOpen }) => (
          <NavButtonItem
            {...props}
            data-project-switcher
            icon="projects"
            label={label}
            isCompact={isCompact}
            isOpen={isOpen}
            isProminent
            trailing={<Icon glyph="chevron-down" size="small" className="text-icon-subtle" />}
          >
            {projectName ?? <Skeleton width="6rem" height="0.875rem" />}
          </NavButtonItem>
        ),
      }}
    />
  );
}
