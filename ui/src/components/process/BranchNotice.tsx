import type { ReadinessItem } from "../../api/types";
import { RemedyCommand, RowMessage } from "../configuration/RowMessage";
import { type BranchState, branchState } from "../configuration/readiness";
import { ServerText } from "../ui";

export { type BranchState, branchState };

const UNKNOWN_HEAD_TITLE = "Review the factorial branch before Factorial syncs it";

/** What is wrong with the branch the config lives on, above the config it holds. */
export function BranchNotice({
  row,
  onCreate,
  onReview,
  noLanes = false,
}: {
  row: ReadinessItem | undefined;
  /** Absent for a reader who may not create it. */
  onCreate?: () => void;
  /** Present while the head is a direct push someone can review and adopt. */
  onReview?: () => void;
  /** Nothing is synced yet, so an unknown head's notice says so before the server's detail. */
  noLanes?: boolean;
}) {
  const state = branchState(row);
  if (!row || state === "checking" || state === "ok") return null;
  if (state === "not-checked") {
    return (
      <p className="px-4 pt-4 text-body-sm text-subtle">
        {row.detail ? <ServerText text={row.detail} /> : row.summary}
      </p>
    );
  }
  const unknownHead = state === "unknown-head";
  const shown =
    unknownHead && noLanes
      ? { ...row, detail: `No process is synced yet. ${row.detail ?? ""}`.trim() }
      : row;
  return (
    <div className="space-y-3 border-b border-default p-4">
      <RowMessage
        row={shown}
        title={unknownHead ? UNKNOWN_HEAD_TITLE : row.summary}
        actions={
          state === "missing" && onCreate
            ? [{ text: "Create the branch", onClick: onCreate }]
            : unknownHead && onReview
              ? [{ text: "Review and adopt", onClick: onReview }]
              : undefined
        }
      />
      {row.remedy && (
        <RemedyCommand remedy={row.remedy} label="Command to reset the factorial branch" />
      )}
    </div>
  );
}
