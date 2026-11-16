/**
 * What every page of a project shares: the project's own read, and the states before it lands —
 * a bad id, a project that is gone, a read in flight or failed. Each page draws its own header
 * once the project is there; before that, they all look the same, so a reader moving between a
 * project's pages sees one frame. Where the page sits is the top bar's to say: the organization
 * and then the project, each with its switcher.
 */

import type { UseQueryResult } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { isNotFound } from "../../api/client";
import { useProject } from "../../api/queries";
import type { ProjectDetail } from "../../api/types";
import { EmptyState, ErrorMessage, LinkButton, PageHeader } from "../ui";

export function useProjectRoute(): {
  projectId: number;
  query: UseQueryResult<ProjectDetail>;
  project: ProjectDetail | undefined;
} {
  const raw = Number(useParams().projectId);
  const projectId = Number.isInteger(raw) && raw > 0 ? raw : 0;
  const query = useProject(projectId);
  return { projectId, query, project: query.data };
}

/** The page while the project is not there to draw: not found, loading, or failed with nothing
 *  cached. A page renders this in place of itself until `project` is defined. */
export function ProjectPageState({
  projectId,
  query,
}: {
  projectId: number;
  query: UseQueryResult<ProjectDetail>;
}) {
  if (projectId === 0 || (query.error && isNotFound(query.error))) {
    return (
      <div className="space-y-6">
        <PageHeader title="Project not found" />
        <EmptyState
          header="Factorial can't find that project"
          description="It may have been deleted, or the link is wrong."
          primaryAction={<LinkButton to="/projects">Back to projects</LinkButton>}
        />
      </div>
    );
  }
  return (
    <div className="space-y-6">
      <PageHeader title="" isLoading={!query.error} />
      {query.error ? (
        <ErrorMessage
          error={query.error}
          title="Couldn't load this project"
          onRetry={() => query.refetch()}
        />
      ) : null}
    </div>
  );
}
