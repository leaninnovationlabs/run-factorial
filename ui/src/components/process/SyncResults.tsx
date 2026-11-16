import type { ProcessSync, ProcessSyncResult } from "../../api/types";
import { shortSha } from "../../format";
import { Code, Lozenge, SectionMessage } from "../ui";

/**
 * What one sync did, lane by lane — every lane the repo offers, not just the one on the stage,
 * because one commit is read for all of them. `unchanged` is the ordinary outcome for a lane the
 * commit did not touch, and naming it is what stops it reading as a failure. The success closes
 * once read; a warning under it stays. Whoever holds the result holds the closing too
 * (`useDismissible`), so a parent that unmounts this does not bring a closed message back.
 */
const OUTCOME: Record<ProcessSyncResult["outcome"], { label: string; created: boolean }> = {
  created: { label: "Updated", created: true },
  unchanged: { label: "No changes", created: false },
  retired: { label: "Removed", created: false },
};

export function SyncResults({
  result,
  adopted = false,
  shown,
  onDismiss,
}: {
  result: ProcessSync;
  adopted?: boolean;
  /** False once the success was closed. */
  shown: boolean;
  onDismiss: () => void;
}) {
  // Nothing left at all, not an empty box: the messages around it hide when they are empty.
  if (!shown && !result.default_process_moved && result.agent_warnings.length === 0) return null;
  return (
    <div className="space-y-2">
      {shown && (
        <SectionMessage
          appearance="success"
          onDismiss={onDismiss}
          title={
            adopted ? (
              <>
                Adopted <Code>{shortSha(result.sha)}</Code> and synced from it
              </>
            ) : (
              <>
                Synced from <Code>{shortSha(result.sha)}</Code>
              </>
            )
          }
        >
          <ul className="space-y-1">
            {result.results.map((row) => (
              <li key={row.process} className="flex items-center gap-2">
                <Code>{row.process}</Code>
                <Lozenge appearance={OUTCOME[row.outcome].created ? "success" : "neutral"}>
                  {OUTCOME[row.outcome].label}
                </Lozenge>
              </li>
            ))}
          </ul>
        </SectionMessage>
      )}
      {result.default_process_moved && (
        <SectionMessage appearance="warning" title="The default process moved">
          <Code>{result.default_process_moved.previous}</Code> was removed, so{" "}
          <Code>{result.default_process_moved.now}</Code> is now the default process. Use Set as
          default to change it.
        </SectionMessage>
      )}
      {result.agent_warnings.length > 0 && (
        <SectionMessage appearance="warning" title="Some agents need attention">
          <ul className="list-disc space-y-1 pl-4">
            {result.agent_warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </SectionMessage>
      )}
    </div>
  );
}
