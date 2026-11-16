import { safeGet } from "../../storage";
import { readBundleDraft } from "./explorer/bundleDraft";
import { readCreatedFiles } from "./explorer/createdFiles";
import { readDeletedFiles } from "./explorer/deletedFiles";
import { readRepoEdits } from "./explorer/repoEdits";
import { key } from "./processDraft";

/** Whether the editor holds uncommitted work for a lane (`null`: one not written yet) — its
 *  process text, an edited file, a new one or a staged deletion. Read from storage, so the section can say so once the drawer
 *  has closed. The exact key only: a pre-lanes draft would otherwise count for every lane. */
export function hasPendingEdits(projectId: number, processKey: string | null): boolean {
  return (
    safeGet(key(projectId, processKey)) !== null ||
    Object.keys(readBundleDraft(projectId, processKey) ?? {}).length > 0 ||
    Object.keys(readRepoEdits(projectId, processKey)).length > 0 ||
    Object.keys(readCreatedFiles(projectId, processKey)).length > 0 ||
    Object.keys(readDeletedFiles(projectId, processKey)).length > 0
  );
}
