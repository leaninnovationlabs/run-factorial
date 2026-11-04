/**
 * A project's home: its header — admission, refresh, repository, spend — and the dashboard of what
 * it spent and delivered (`DashboardView`). A project with a blocker says so here and points at the
 * configuration page that clears it; the configuration itself lives on its own pages.
 */

import { Navigate, useSearchParams } from "react-router-dom";
import { useCan } from "../api/organizationQueries";
import { useBoard } from "../api/queries";
import type { ProjectSpend } from "../api/types";
import { AdmissionLozenge } from "../components/configuration/AdmissionLozenge";
import { RepoLink } from "../components/configuration/RepoLink";
import { blockingRows, ROW_TITLES } from "../components/configuration/readiness";
import { type DashboardSection, DashboardView } from "../components/dashboard/DashboardView";
import {
  AdmissionControl,
  firstBlockerHref,
  legacyTabHref,
  ProjectPageState,
  projectHref,
  useProjectRoute,
} from "../components/project";
import { FleetRefresh } from "../components/tickets/FleetRefresh";
import { SpendFigures } from "../components/tickets/SpendFigures";
import { PageHeader, SectionMessage, staleSince, usePageTitle } from "../components/ui";
import { formatCost, formatTokens } from "../format";
import { useViewParams } from "../searchParams";

const SECTIONS = new Set<string>(["spend", "time", "activity"]);

/** The dashboard's section lives in the URL, so a view can be shared by link and survives a
 *  reload. Anything unrecognised reads as the default. */
function useDashboardSection() {
  const [params, update] = useViewParams();
  const section = params.get("section");
  return {
    section: section && SECTIONS.has(section) ? (section as DashboardSection) : "spend",
    setSection: (value: DashboardSection) => update({ section: value === "spend" ? null : value }),
  };
}

export function ProjectDashboardPage() {
  const { projectId, query, project } = useProjectRoute();
  const { section, setSection } = useDashboardSection();
  const board = useBoard(projectId);
  const canConfigure = useCan("projects:manage");
  const canReadConfig = useCan("projects:read");
  usePageTitle([project?.name, "Projects"]);
  const [params] = useSearchParams();
  const draft = params.get("draft");
  const legacy = legacyTabHref(projectHref(projectId), params.get("tab"), draft ? "process" : null);

  // A bookmark or an editor card from before a project had pages, carrying its draft along.
  if (legacy) return <Navigate replace to={draft ? `${legacy}?draft=${draft}` : legacy} />;
  if (!project) return <ProjectPageState projectId={projectId} query={query} />;

  const blocking = blockingRows(project);
  const ready = project.blockers.length === 0;

  return (
    <div className="space-y-6">
      <PageHeader
        title={project.name}
        titleAfter={<AdmissionLozenge active={project.active} />}
        actions={
          <div className="flex max-w-3xl flex-wrap items-center justify-end gap-2">
            <FleetRefresh projectId={project.id} />
            {canConfigure && <AdmissionControl project={project} />}
          </div>
        }
        bottomBar={
          <>
            <RepoLink repo={project.github_repo} />
            {project.description && <span className="text-subtle">{project.description}</span>}
            {/* What a project has cost is a fact about the project, so it sits in the header
                rather than on the tickets page. From the board, which polls — see `BoardResponse`. */}
            <Spend spend={board.data?.spend} />
          </>
        }
        staleSince={staleSince(query)}
      />

      {!ready && !project.active && (
        <SectionMessage
          appearance="warning"
          title="Finish configuration to activate"
          actions={
            canReadConfig ? [{ text: "View project settings", to: firstBlockerHref(project) }] : []
          }
        >
          {blocking.length > 0 ? (
            <ul className="list-disc space-y-0.5 pl-4">
              {blocking.map((row) => (
                <li key={`${row.key}-${row.process ?? ""}`}>
                  <span className="font-medium">{ROW_TITLES[row.key]}</span>: {row.summary}
                </li>
              ))}
            </ul>
          ) : (
            project.blockers.join(" ")
          )}
        </SectionMessage>
      )}

      <DashboardView projectId={project.id} section={section} onSectionChange={setSection} />
    </div>
  );
}

/**
 * What the project has spent, across every attempt of every ticket, stated the way a ticket's
 * own page states it for one ticket. Absent until there is something to say: a project nobody
 * has run yet would draw three boxes to say what the empty dashboard below already says.
 */
function Spend({ spend }: { spend: ProjectSpend | undefined }) {
  if (!spend) return null;
  if (spend.input_tokens + spend.output_tokens === 0 && Number(spend.cost_usd) === 0) return null;
  return (
    <span data-assist-anchor="project.spend" className="inline-flex">
      <SpendFigures
        tokens={formatTokens(spend.input_tokens, spend.output_tokens)}
        // The sum coalesces unpriced runs to zero; a project that only ran those has spent tokens
        // and no known dollars, which is a dash rather than a free project.
        cost={formatCost(
          Number(spend.cost_usd) === 0 ? null : spend.cost_usd,
          spend.cost_estimated,
        )}
        estimated={spend.cost_estimated}
      />
    </span>
  );
}
