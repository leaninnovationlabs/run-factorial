/**
 * The Project settings overview's ledger: one row per configuration page, each with its readiness
 * and a way in. It is the checklist a new project is finished against, read top to bottom in the
 * sections' order, and the place that says which rows are live GitHub reads and whether they have
 * answered yet (`UNCHECKED`).
 */

import { Link } from "react-router-dom";
import { useCan } from "../../api/organizationQueries";
import { useRuntimes } from "../../api/queries";
import type { ProjectDetail, ProjectReadiness, ReadinessItem } from "../../api/types";
import { configurationHref } from "../project/place";
import { Icon, Lozenge, Panel, ServerText } from "../ui";
import { runTimeStatus } from "./PermitsBody";
import {
  CHECKING,
  type READINESS,
  readinessOf,
  type Section,
  sectionRows,
  UNCHECKED,
} from "./readiness";

type Status = (typeof READINESS)[ReadinessItem["state"]] & {
  summary: string;
  /** The row is a probe nobody has run: no verdict to put a lozenge on. */
  unchecked: boolean;
};

export function ConfigurationLedger({
  project,
  probes,
  checking,
}: {
  project: ProjectDetail;
  /** The repo-side rows, once someone asked for them. */
  probes: ProjectReadiness | undefined;
  /** The probes are on their way: their rows say so instead of that nobody asked. */
  checking: boolean;
}) {
  const rows = sectionRows(project, probes, checking ? CHECKING : UNCHECKED);
  const canRuntimes = useCan("runtimes:manage");
  const runtimes = useRuntimes({ enabled: canRuntimes }).data;
  return (
    <Panel title="Requirements">
      <ul className="divide-y divide-default">
        {rows.map(({ section, row }) => {
          const status: Status | null =
            section.slug === "runtime"
              ? {
                  ...runTimeStatus(project, row, canRuntimes ? runtimes : null),
                  unchecked: false,
                }
              : row
                ? {
                    ...readinessOf(row.key, row.state),
                    summary: row.summary,
                    unchecked: row.summary === UNCHECKED,
                  }
                : null;
          return (
            <LedgerRow
              key={section.slug}
              projectId={project.id}
              section={section}
              status={status}
            />
          );
        })}
      </ul>
    </Panel>
  );
}

function LedgerRow({
  projectId,
  section,
  status,
}: {
  projectId: number;
  section: Section;
  status: Status | null;
}) {
  return (
    <li>
      <Link
        to={configurationHref(projectId, section.slug)}
        data-ledger-row={section.slug}
        className="focus-inset flex items-center gap-3 px-4 py-3 text-default transition-colors hover:bg-neutral-subtle-hovered active:bg-neutral-subtle-pressed"
      >
        <span className="flex size-5 shrink-0 items-center justify-center">
          <Icon
            glyph={status ? status.glyph : "chevron-right"}
            label={status?.label}
            className={status ? status.ink : "text-icon-subtle"}
          />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-body font-medium">{section.title}</span>
          <span className="block truncate text-body-sm text-subtle">
            {status ? <ServerText text={status.summary} /> : section.summary}
          </span>
        </span>
        {status &&
          (status.unchecked ? (
            <span className="text-body-sm text-subtlest">Checked when you open it</span>
          ) : (
            <Lozenge appearance={status.appearance} isQuiet>
              {status.label}
            </Lozenge>
          ))}
      </Link>
    </li>
  );
}
