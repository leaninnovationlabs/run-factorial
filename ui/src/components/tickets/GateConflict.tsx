import { useState } from "react";
import { newIdempotencyKey } from "../../api/idempotency";
import { useRequestConflictFix } from "../../api/queries";
import type { StepRun } from "../../api/types";
import { CONFLICT_FIX_REFUSALS } from "../activity/words";
import { Button, ErrorMessage, Lozenge } from "../ui";

/**
 * Why a waiting gate's pull request cannot merge as it stands, and the one press that asks for
 * an agent to fix it. The key is made once and kept through a failed send, so a retried press is
 * the same request to the API; an answer, even a refusal, gets the next press a new key.
 */
export function GateConflict({ ticketId, run }: { ticketId: number; run: StepRun }) {
  const [key, setKey] = useState(newIdempotencyKey);
  const [refusal, setRefusal] = useState<string | null>(null);
  const request = useRequestConflictFix(ticketId, (result) => {
    setKey(newIdempotencyKey());
    if (result.outcome === "refused" && result.reason) {
      setRefusal(
        `Conflict fix not started: ${CONFLICT_FIX_REFUSALS[result.reason] ?? result.reason}.`,
      );
    }
  });
  if (run.status !== "waiting" || run.mergeability === null) return null;

  const base = run.base_ref ?? "its target";
  const waiting = run.conflict_fix_pending || request.isPending;
  return (
    <div className="mt-2 space-y-2">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <Lozenge appearance="warning">
          {run.mergeability === "conflicting" ? `Conflicts with ${base}` : `Behind ${base}`}
        </Lozenge>
        {run.can_fix_conflict ? (
          <>
            <Button
              spacing="compact"
              isDisabled={waiting}
              onClick={() => {
                setRefusal(null);
                request.mutate({ stepRunId: run.id, idempotencyKey: key });
              }}
            >
              {run.mergeability === "conflicting" ? "Fix conflicts" : "Update branch"}
            </Button>
            {waiting && (
              <span role="status" className="text-body-sm text-subtle">
                Waiting for the next check of the pull request.
              </span>
            )}
          </>
        ) : (
          <span className="text-body-sm text-subtle">
            This lane has no step that can fix it. Merge the target branch into the pull request by
            hand.
          </span>
        )}
      </div>
      <ErrorMessage error={request.error} title="Couldn't ask for a conflict fix" />
      {refusal && (
        <span role="status" className="block text-body-sm text-subtle">
          {refusal}
        </span>
      )}
    </div>
  );
}
