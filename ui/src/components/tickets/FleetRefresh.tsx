import { useRefreshFleet } from "../../api/queries";
import { ActionError, Button, useActionFeedback } from "../ui";

/**
 * The project pages' refresh: a fleet pass now, so an issue opened since the last tick is
 * admitted without waiting out the interval. A fragment of a button and its error, for a
 * wrapping row of actions; the error takes a line of its own below the buttons.
 */
export function FleetRefresh({ projectId }: { projectId?: number }) {
  const refresh = useRefreshFleet(projectId);
  useActionFeedback(refresh, { errorTitle: "Couldn't refresh the tickets" });
  return (
    <>
      <Button
        iconBefore="refresh"
        aria-label={projectId === undefined ? "Refresh projects" : "Refresh project"}
        isLoading={refresh.isPending}
        onClick={() => refresh.mutate()}
      >
        Refresh
      </Button>
      {refresh.error && (
        <span className="order-last basis-full text-right">
          <ActionError error={refresh.error} />
        </span>
      )}
    </>
  );
}
