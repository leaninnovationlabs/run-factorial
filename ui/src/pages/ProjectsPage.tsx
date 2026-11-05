import { useState } from "react";
import { Link } from "react-router-dom";
import { useCan } from "../api/organizationQueries";
import { useProjects } from "../api/queries";
import { ActiveFilter, shows, useActiveView } from "../components/configuration/ActiveFilter";
import { AdmissionLozenge } from "../components/configuration/AdmissionLozenge";
import { RepoLink } from "../components/configuration/RepoLink";
import { FleetRefresh } from "../components/tickets/FleetRefresh";
import {
  Button,
  Code,
  DataTable,
  EmptyState,
  ErrorMessage,
  Icon,
  LinkButton,
  Lozenge,
  PageHeader,
  Panel,
  RelativeTime,
  staleSince,
  TextField,
  usePageTitle,
} from "../components/ui";
import { forgeHost } from "../forge";

type ProjectSummary = NonNullable<ReturnType<typeof useProjects>["data"]>[number];

const HEAD = [
  { key: "name", content: "Name" },
  { key: "repo", content: "Repository" },
  { key: "runtime", content: "Runtime" },
  { key: "admission", content: "Admission" },
  { key: "created", content: "Created" },
] as const;

/** A filter is worth its row only once the list is longer than a glance. */
const FILTER_FROM = 10;

export function ProjectsPage() {
  const query = useProjects();
  const canCreate = useCan("projects:manage");
  const canRuntimes = useCan("runtimes:manage");
  const [filter, setFilter] = useState("");
  const [admission, setAdmission] = useActiveView();
  usePageTitle(["Projects"]);

  const all = query.data;
  const count = all?.length ?? 0;
  const needle = filter.trim().toLowerCase();
  const rows = all?.filter(
    (project) =>
      shows(admission, project.active) &&
      (!needle ||
        `${project.name} ${project.github_repo} ${project.description ?? ""}`
          .toLowerCase()
          .includes(needle)),
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="Projects"
        actions={
          <div className="flex max-w-3xl flex-wrap items-center justify-end gap-2">
            <FleetRefresh />
            {canCreate && (
              <LinkButton to="/projects/new" appearance="primary" iconBefore="plus">
                Create project
              </LinkButton>
            )}
          </div>
        }
        bottomBar={
          <>
            <ActiveFilter value={admission} onChange={setAdmission} projects={all ?? []} />
            {count > FILTER_FROM && (
              <div className="w-72">
                <TextField
                  label="Filter projects"
                  labelHidden
                  placeholder="Filter by name or repository"
                  value={filter}
                  onChange={(event) => setFilter(event.target.value)}
                  elemBeforeInput={<Icon glyph="search" />}
                />
              </div>
            )}
          </>
        }
        staleSince={staleSince(query)}
      />

      {query.error && !all ? (
        <ErrorMessage
          error={query.error}
          title="Couldn't load the projects"
          onRetry={() => query.refetch()}
        />
      ) : (
        <Panel>
          <DataTable
            label="Projects"
            head={[...HEAD]}
            rows={rows}
            isLoading={query.isPending}
            rowKey={(project) => project.id}
            rowHref={(project) => `/projects/${project.id}`}
            // With projects to list, an empty table can only be the filters' doing.
            isFiltered={count > 0}
            emptyView={
              <EmptyState
                header="No projects yet"
                description={
                  canCreate
                    ? "Create one to connect Factorial to a repository."
                    : "An admin creates the organization's projects."
                }
                primaryAction={
                  canCreate && (
                    <LinkButton to="/projects/new" appearance="primary">
                      Create project
                    </LinkButton>
                  )
                }
              />
            }
            noResultsView={
              <EmptyState
                header="No projects match this filter"
                primaryAction={
                  <Button
                    onClick={() => {
                      setFilter("");
                      setAdmission("all");
                    }}
                  >
                    Clear filters
                  </Button>
                }
              />
            }
            cells={(project) => ({
              name: (
                <div className="min-w-0">
                  <Link to={`/projects/${project.id}`} className="font-medium hover:underline">
                    {project.name}
                  </Link>
                  {project.description && (
                    <p className="text-body-sm text-subtle">{project.description}</p>
                  )}
                </div>
              ),
              repo: (
                <RepoLink
                  repo={project.github_repo}
                  forge={forgeHost(project.ticket_source, project.settings, project.forge)}
                />
              ),
              runtime: <RuntimeCell runtime={project.runtime} isLinked={canRuntimes} />,
              admission: <AdmissionLozenge active={project.active} />,
              created: <RelativeTime iso={project.created_at} />,
            })}
          />
        </Panel>
      )}
    </div>
  );
}

/** The runtime the latest process version names: its name, linked, when the row exists; the
 *  slug and "Missing" when the process names one that has since been deleted (readiness says so
 *  on the project page); "None" before a version names one. */
function RuntimeCell({
  runtime,
  isLinked,
}: {
  runtime: ProjectSummary["runtime"];
  /** False for a reader who cannot open the runtime's page. */
  isLinked: boolean;
}) {
  if (runtime === null) return <span className="text-subtle">None</span>;
  if (runtime.name === null) {
    return (
      <span className="inline-flex items-center gap-2">
        <Code>{runtime.slug}</Code>
        <Lozenge>Missing</Lozenge>
      </span>
    );
  }
  if (!isLinked) return <span>{runtime.name}</span>;
  return (
    <Link to={`/runtimes/${runtime.slug}`} className="hover:underline">
      {runtime.name}
    </Link>
  );
}
