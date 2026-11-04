import type { ConfigCommitResult, ProcessValidateResult, SelectedFile } from "../../api/types";
import { shortSha } from "../../format";
import { asSentence } from "../activity/words";
import { Code, ErrorMessage, SectionMessage } from "../ui";
import { stubbedManifests } from "./commitGate";
import { SyncResults } from "./SyncResults";

/** What the commit button has to say: the commit and its sync, a request that failed, and the
 *  config's own errors and warnings. */
export function EditorMessages({
  committed,
  sent,
  requestError,
  verdict,
}: {
  committed: ConfigCommitResult | undefined;
  /** The files the commit was asked to carry. */
  sent: SelectedFile[];
  requestError: unknown;
  verdict: ProcessValidateResult | undefined;
}) {
  // A no-op commit wrote nothing on purpose, so its empty `written` names no gap.
  const wrote = committed?.committed ? committed : undefined;
  const stubs = wrote ? stubbedManifests(wrote.written, sent) : [];
  const missing = wrote
    ? sent.map((file) => file.path).filter((path) => !wrote.written.includes(path))
    : [];
  const sha = committed ? shortSha(committed.sha) : "";
  const branch = committed?.branch;
  const nothing = `Nothing to commit. The ${branch} branch already holds this`;
  const synced = committed?.committed
    ? `Committed ${sha} to ${branch} and synced`
    : `Synced ${sha}`;
  const syncedNote = committed?.committed
    ? "New tickets run it."
    : `${nothing}. New tickets run it.`;
  const unsynced = committed?.committed
    ? `Committed ${sha} to ${branch}, but the sync failed`
    : `${nothing}, but the sync of ${sha} failed`;
  return (
    <div className="max-h-56 space-y-2 overflow-y-auto border-b border-default px-4 py-3 empty:hidden">
      {committed &&
        (committed.sync_error === null ? (
          <SectionMessage
            appearance="success"
            title={synced}
            actions={[{ text: "View commit", href: committed.url }]}
          >
            {syncedNote}
          </SectionMessage>
        ) : (
          <SectionMessage
            appearance="warning"
            isAlert
            actions={[{ text: "View commit", href: committed.url }]}
          >
            {`${unsynced}: ${asSentence(committed.sync_error)} Press Back, then press Sync config to try again.`}
          </SectionMessage>
        ))}
      {committed?.sync && <SyncResults result={committed.sync} />}
      {stubs.length > 0 && (
        <SectionMessage appearance="warning" title="Some agents are unfinished">
          {stubs.map((path, index) => (
            <span key={path}>
              {index > 0 && ", "}
              <Code>{path}</Code>
            </span>
          ))}{" "}
          {stubs.length === 1 ? "has" : "have"} no prompts, so{" "}
          {stubs.length === 1 ? "its" : "their"} <Code>agent.yaml</Code> is comments only. Fill{" "}
          {stubs.length === 1 ? "it" : "them"} in before you activate the project. A step that uses
          an unfinished agent fails when it starts.
        </SectionMessage>
      )}
      {missing.length > 0 && (
        <SectionMessage appearance="error" isAlert title="Some edits are missing from the commit">
          {missing.map((path, index) => (
            <span key={path}>
              {index > 0 && ", "}
              <Code>{path}</Code>
            </span>
          ))}{" "}
          {missing.length === 1 ? "was" : "were"} sent but not written. Your edit is still here.
          Commit again.
        </SectionMessage>
      )}
      <ErrorMessage error={requestError} />
      {verdict && verdict.errors.length > 0 && (
        <SectionMessage appearance="error" title="The config has errors">
          <ul className="space-y-1">
            {verdict.errors.map((error) => (
              <li key={error} className="font-mono text-body-sm whitespace-pre-wrap">
                {error}
              </li>
            ))}
          </ul>
        </SectionMessage>
      )}
      {verdict && verdict.warnings.length > 0 && (
        <SectionMessage appearance="warning" title="Warnings">
          <ul className="space-y-2">
            {verdict.warnings.map((warning) => (
              <li key={`${warning.code}-${warning.loc}`}>
                <span className="flex flex-wrap gap-2">
                  <Code>{warning.code}</Code>
                  <Code>{warning.loc}</Code>
                </span>
                <span className="mt-1 block">{warning.msg}</span>
              </li>
            ))}
          </ul>
        </SectionMessage>
      )}
    </div>
  );
}
