import { useQueries } from "@tanstack/react-query";
import { api } from "../../api/client";
import { keys, useProject } from "../../api/queries";
import { isProcessConfigLike } from "../flow/processGraph";

/**
 * The agent steps in process order, default lane first: what keys a step's colour, so a step
 * keeps it whatever the window. The dashboard's own `steps` are in first-start order over the
 * window, so a step with no run in it would hand its colour to the next. Read from each lane's
 * newest pinned version, which never changes, so it is fetched once; empty until it lands.
 */
export function useAgentStepOrder(projectId: number): string[] {
  const lanes = [...(useProject(projectId).data?.processes ?? [])].sort(
    (a, b) => Number(b.is_default) - Number(a.is_default),
  );
  const versions = useQueries({
    queries: lanes.map((lane) => ({
      queryKey: keys.processVersion(projectId, lane.latest.id),
      queryFn: () => api.getProcessVersion(projectId, lane.latest.id),
      staleTime: Number.POSITIVE_INFINITY,
    })),
  });
  return versions.flatMap(({ data }) =>
    data && isProcessConfigLike(data.content)
      ? data.content.steps.filter((step) => step.kind === "agent").map((step) => step.name)
      : [],
  );
}
