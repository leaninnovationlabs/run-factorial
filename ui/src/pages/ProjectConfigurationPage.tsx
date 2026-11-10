/**
 * The Project settings overview: every requirement in reading order, each a way to its page. The
 * repo-side probes — live GitHub reads of the App installation, the labels, the branch protection,
 * the CI workflows — run when this page opens, as on the page of one of them; the last answer shows
 * until the new one lands, and Check repository runs them again. Check repository only reads, so a
 * reader without `projects:manage` has it too; admission is theirs only with it.
 */

import { useCan } from "../api/organizationQueries";
import { useProjectReadiness } from "../api/queries";
import { AdmissionLozenge } from "../components/configuration/AdmissionLozenge";
import { ConfigurationLedger } from "../components/configuration/ReadinessLedger";
import { blockingRows, ROW_TITLES } from "../components/configuration/readiness";
import {
  AdmissionControl,
  firstBlockerHref,
  ProjectPageState,
  useProjectRoute,
} from "../components/project";
import {
  Button,
  ErrorMessage,
  PageHeader,
  RelativeTime,
  SectionMessage,
  staleSince,
  usePageTitle,
} from "../components/ui";

export function ProjectConfigurationPage() {
  const { projectId, query, project } = useProjectRoute();
  const probes = useProjectReadiness(projectId);
  const canManage = useCan("projects:manage");
  usePageTitle(["Project settings", project?.name]);

  if (!project) return <ProjectPageState projectId={projectId} query={query} />;

  const blocking = blockingRows(project);
  const ready = project.blockers.length === 0;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Project settings"
        titleAfter={<AdmissionLozenge active={project.active} isQuiet />}
        actions={
          <div className="flex flex-wrap items-center justify-end gap-2">
            <span data-assist-anchor="configuration.check" className="inline-flex">
              <Button
                iconBefore="refresh"
                isLoading={probes.isFetching}
                onClick={() => probes.refetch()}
              >
                Check repository
              </Button>
            </span>
            {canManage && <AdmissionControl project={project} />}
          </div>
        }
        bottomBar={
          probes.data ? (
            <span>
              Repository checked <RelativeTime iso={new Date(probes.dataUpdatedAt).toISOString()} />
            </span>
          ) : (
            <span>
              When this page opens, it reads these from GitHub: the GitHub App's access to the
              repository, the config branch, the ticket labels, branch protection and the CI
              workflows.
            </span>
          )
        }
        staleSince={staleSince(query)}
      />

      <ErrorMessage
        error={probes.error}
        title="Couldn't check the repository"
        onRetry={() => probes.refetch()}
      />

      {!ready && !project.active && (
        <SectionMessage
          appearance="warning"
          title="Finish configuration to activate"
          actions={[{ text: "Go to the first requirement", to: firstBlockerHref(project) }]}
          actionsAs="buttons"
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

      <ConfigurationLedger project={project} probes={probes.data} checking={probes.isFetching} />
    </div>
  );
}
