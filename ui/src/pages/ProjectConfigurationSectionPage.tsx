/**
 * One configuration page: its title, the readiness of the requirements it holds, and their forms.
 * A page with a live GitHub read among its rows asks for the probes on arrival (`Section.probe`);
 * every other page reads only what is cached, so opening Secrets costs the repository nothing.
 *
 * What is wrong leads the page, under the header and above every form, one message per row that
 * is not ready, each with its fix as a button (`PageMessages`). The header's state is the page's
 * worst row. The process editor is the process config page's body, because syncing a snapshot
 * and drafting the file are two halves of the same requirement. While it is open it is the whole
 * page: the header and the messages step aside (`onFullPage`), and its Back brings them back.
 *
 * A reader who holds `projects:read` without `projects:manage` gets every page with what it says
 * and none of what writes: each body and `PageMessages` leave their writing controls out.
 */

import { type ComponentType, useState } from "react";
import { Navigate, useParams } from "react-router-dom";
import { useCan } from "../api/organizationQueries";
import { useProjectReadiness, useRuntimes } from "../api/queries";
import type { ProjectDetail, ReadinessItem } from "../api/types";
import { PermitsBody, runTimeStatus } from "../components/configuration/PermitsBody";
import {
  ProjectDetailsBody,
  RepositorySetupBody,
  RuntimeBody,
  TicketSourceBody,
} from "../components/configuration/ProjectConfig";
import { PageMessages } from "../components/configuration/RowMessage";
import {
  branchRow,
  CHECKING,
  NEVER_BLOCKS,
  readinessOf,
  retiredSection,
  type Section,
  type SectionSlug,
  saysHeadReview,
  sectionBySlug,
  sectionRows,
  UNCHECKED,
} from "../components/configuration/readiness";
import { SecretsBody } from "../components/configuration/SecretsBody";
import { ProcessSection } from "../components/process/ProcessSection";
import { configurationHref, ProjectPageState, useProjectRoute } from "../components/project";
import {
  EmptyState,
  ErrorMessage,
  InlineMessage,
  LinkButton,
  Lozenge,
  PageHeader,
  Panel,
  ServerText,
  staleSince,
  usePageTitle,
} from "../components/ui";

type BodyProps = {
  project: ProjectDetail;
  /** The page's rows; empty for a page whose rows the server did not send. */
  rows: ReadinessItem[];
  branch?: ReadinessItem;
  onFullPage: (on: boolean) => void;
  /** The header's actions, for a body whose controls act on the whole page. */
  actionsSlot: HTMLElement | null;
};

const BODIES: Record<SectionSlug, ComponentType<BodyProps>> = {
  details: ({ project }) => <ProjectDetailsBody project={project} />,
  "repository-setup": RepositorySetupBody,
  "ticket-source": TicketSourceBody,
  process: ({ project, branch, onFullPage, actionsSlot }) => (
    <ProcessSection
      project={project}
      branch={branch}
      onFullPage={onFullPage}
      actionsSlot={actionsSlot}
    />
  ),
  // The runtime each process names, when one does, then what the project permits them.
  runtime: ({ project, rows }) => (
    <div className="divide-y divide-default">
      {rows.length > 0 && <RuntimeBody project={project} />}
      <PermitsBody project={project} />
    </div>
  ),
  secrets: ({ project }) => <SecretsBody project={project} />,
};

export function ProjectConfigurationSectionPage() {
  const { projectId, query, project } = useProjectRoute();
  const slug = useParams().section;
  const section = sectionBySlug(slug);
  const retired = retiredSection(slug);
  const probes = useProjectReadiness(projectId, { enabled: section?.probe ?? false });
  const canRuntimes = useCan("runtimes:manage");
  const runtimes = useRuntimes({ enabled: canRuntimes });
  const [fullPage, setFullPage] = useState(false);
  const [actionsSlot, setActionsSlot] = useState<HTMLElement | null>(null);
  usePageTitle([section?.title ?? "Project settings", project?.name]);

  if (retired) return <Navigate to={configurationHref(projectId, retired)} replace />;
  if (!project) return <ProjectPageState projectId={projectId} query={query} />;

  if (!section) {
    return (
      <div className="space-y-6">
        <PageHeader title="Page not found" />
        <EmptyState
          header="Factorial can't find that configuration page"
          description="The link may be old, or the address may be wrong."
          primaryAction={
            <LinkButton to={configurationHref(project.id)}>Back to project settings</LinkButton>
          }
        />
      </div>
    );
  }

  const pending = probes.isFetching ? CHECKING : UNCHECKED;
  const entry = sectionRows(project, probes.data, pending).find(
    (candidate) => candidate.section.slug === section.slug,
  );
  const row = entry?.row ?? null;
  const rows = entry?.rows ?? [];
  const reported = branchRow(project.readiness, probes.data, project.probe_keys, pending);
  // The probe that follows a connection answers "not checked" until it lands; a Sync pressed on
  // that answer is refused for a head it has not yet read.
  const branch =
    probes.isFetching && reported?.state === "unknown"
      ? { ...reported, summary: CHECKING }
      : reported;
  const status = statusOf(section, row, project, canRuntimes ? runtimes.data : null);
  const neverBlocks = rows.length > 0 && rows.every((own) => NEVER_BLOCKS.has(own.key));

  return (
    <div className="space-y-6">
      {/* Hidden rather than unmounted around the body: a body that moved in the tree would
          remount, and lose the editor it just opened. */}
      <div hidden={fullPage} className="space-y-6">
        <PageHeader
          title={section.title}
          actions={
            section.slug === "process" && (
              <span ref={setActionsSlot} className="flex flex-wrap items-center gap-2" />
            )
          }
          titleAfter={
            status && (
              <span className="flex items-center gap-2">
                <Lozenge appearance={status.appearance} iconBefore={status.glyph} isQuiet>
                  {status.label}
                </Lozenge>
                {neverBlocks && row?.state !== "blocked" && (
                  <span className="text-body-sm text-subtlest">Never blocks activation</span>
                )}
              </span>
            )
          }
          bottomBar={
            status && (
              <span className="flex flex-wrap items-center gap-x-2">
                {/* A row that needs action says so once, in the box below or under its field,
                  so the header does not repeat it ("never synced" twice over). */}
                {!(row && (row.state === "blocked" || row.state === "warn")) && (
                  <ServerText text={status.summary} />
                )}
                {/* A ready or unchecked row's detail is a footnote; one that needs action leads the
                  page as a message instead, beside its fix. */}
                {row?.detail && (row.state === "ok" || row.state === "unknown") && (
                  <InlineMessage iconLabel={`About ${section.title}`} title={row.summary}>
                    <ServerText text={row.detail} />
                  </InlineMessage>
                )}
              </span>
            )
          }
          staleSince={staleSince(query)}
        />
        {section.probe && (
          <ErrorMessage
            error={probes.error}
            title="Couldn't check the repository"
            onRetry={() => probes.refetch()}
          />
        )}
        <PageMessages
          project={project}
          rows={rows.filter(
            (own) =>
              !section.saidInBody?.includes(own.key) &&
              !(own.key === "process_config" && saysHeadReview(branch)),
          )}
        />
      </div>
      <Panel bare={fullPage}>
        <Body
          section={section}
          project={project}
          rows={rows}
          branch={branch}
          onFullPage={setFullPage}
          actionsSlot={actionsSlot}
        />
      </Panel>
    </div>
  );
}

function statusOf(
  section: Section,
  row: ReadinessItem | null,
  project: ProjectDetail,
  runtimes: ReturnType<typeof useRuntimes>["data"] | null,
) {
  if (section.slug === "runtime") return runTimeStatus(project, row, runtimes);
  if (!row) return null;
  return { ...readinessOf(row.key, row.state), summary: row.summary };
}

function Body({ section, ...props }: BodyProps & { section: Section }) {
  // The server sends a row only when a process makes the requirement real: secrets with nothing
  // declared, say. A page whose form fixes its rows is a page either way.
  if (section.keys.length > 0 && props.rows.length === 0 && !section.alwaysShown) {
    return (
      <EmptyState
        headingLevel={3}
        header="Nothing to check"
        description="None of this project's processes needs this requirement."
      />
    );
  }
  const Component = BODIES[section.slug];
  return <Component {...props} />;
}
