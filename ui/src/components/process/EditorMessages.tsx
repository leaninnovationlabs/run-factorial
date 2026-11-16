import { ApiError } from "../../api/client";
import type {
  ConfigCommitResult,
  DraftCheck,
  ProcessValidateResult,
  SelectedFile,
} from "../../api/types";
import { errorHint, errorText, shortSha } from "../../format";
import { asSentence } from "../activity/words";
import { Button, Code, ErrorMessage, SectionMessage, ServerText, useDismissible } from "../ui";
import { stubbedManifests } from "./commitGate";
import { SyncResults } from "./SyncResults";

const fileName = (path: string) => path.slice(path.lastIndexOf("/") + 1);
const count = (n: number, noun: string) => `${n} ${noun}${n === 1 ? "" : "s"}`;

/** What the commit button has to say: the commit and its sync, the whole draft's verdict when
 *  Validate was pressed, a request that failed, and the process file's own errors and warnings. */
export function EditorMessages({
  committed,
  sent,
  requestError,
  verdict,
  deleting = [],
  check,
  onOpenFile,
  missingAgents = [],
  onCreateAgent,
}: {
  committed: ConfigCommitResult | undefined;
  /** The files the commit was asked to carry. */
  sent: SelectedFile[];
  requestError: unknown;
  verdict: ProcessValidateResult | undefined;
  /** Files staged for deletion: what the next commit removes. */
  deleting?: string[];
  /** The whole draft's verdict, while it is still the verdict on the draft on screen. */
  check?: DraftCheck;
  /** Opens a config file by its path; null is the process file. Absent, files are only named. */
  onOpenFile?: (path: string | null) => void;
  /** Agents a step names that the branch, the catalog and the draft all lack. */
  missingAgents?: string[];
  onCreateAgent?: (name: string) => void;
}) {
  const [committedShown, dismissCommitted] = useDismissible(
    committed?.sync_error === null ? committed : undefined,
  );
  const [syncShown, dismissSync] = useDismissible(committed?.sync);
  const [validShown, dismissValid] = useDismissible(check?.valid ? check : undefined);
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
  // A ticket keeps the config it was admitted on (design.md §2 item 8), so a commit never reaches
  // one that is already running; the confirmation says so rather than leaving it to be found out.
  const ran = "New tickets run it. Tickets already running keep the version they were admitted on.";
  const syncedNote = committed?.committed ? ran : `${nothing}. ${ran}`;
  const unsynced = committed?.committed
    ? `Committed ${sha} to ${branch}, but the sync failed`
    : `${nothing}, but the sync of ${sha} failed`;
  return (
    <div className="max-h-56 space-y-2 overflow-y-auto border-b border-default px-4 py-3 empty:hidden">
      {committed &&
        (committed.sync_error === null ? (
          committedShown && (
            <SectionMessage
              appearance="success"
              title={synced}
              actions={[{ text: "View commit", href: committed.url }]}
              onDismiss={dismissCommitted}
            >
              {syncedNote}
            </SectionMessage>
          )
        ) : (
          <SectionMessage
            appearance="warning"
            isAlert
            actions={[{ text: "View commit", href: committed.url }]}
          >
            {`${unsynced}: ${asSentence(committed.sync_error)} Press Back, then press Sync config to try again.`}
          </SectionMessage>
        ))}
      {committed?.sync && (
        <SyncResults
          result={committed.sync}
          shown={syncShown !== undefined}
          onDismiss={dismissSync}
        />
      )}
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
      {missingAgents.map((name) => (
        <SectionMessage
          key={name}
          appearance="warning"
          title={`${name} has no files yet`}
          actions={
            onCreateAgent
              ? [{ text: `Create ${name}`, onClick: () => onCreateAgent(name) }]
              : undefined
          }
        >
          A step names this agent. Without its files the commit writes an empty template, and a step
          that uses it fails when it starts.
        </SectionMessage>
      ))}
      {deleting.length > 0 && (
        <SectionMessage
          appearance="warning"
          title={`The next commit deletes ${deleting.length} ${deleting.length === 1 ? "file" : "files"}`}
        >
          {deleting.map((path, index) => (
            <span key={path}>
              {index > 0 && ", "}
              <Code>{path}</Code>
            </span>
          ))}
        </SectionMessage>
      )}
      {validShown && (
        <SectionMessage
          appearance="success"
          title="The whole config is valid"
          onDismiss={dismissValid}
        >
          {`A commit would write ${count(validShown.would_write.length, "file")} and delete ${validShown.would_delete.length}.`}
        </SectionMessage>
      )}
      {check && !check.valid && (
        <SectionMessage
          appearance="error"
          title={`The config has ${count(check.problems.length, "problem")}`}
        >
          <ul className="space-y-2">
            {check.problems.map((problem) => (
              <li key={`${problem.path}-${problem.message}`}>
                {onOpenFile ? (
                  <Button spacing="compact" onClick={() => onOpenFile(problem.path)}>
                    {problem.path ?? "Process file"}
                  </Button>
                ) : (
                  <Code>{problem.path ?? "Process file"}</Code>
                )}
                <span className="mt-1 block whitespace-pre-wrap">
                  <ServerText text={problem.message} />
                </span>
                {problem.hint && (
                  <span className="block text-subtle">
                    <ServerText text={problem.hint} />
                  </span>
                )}
              </li>
            ))}
          </ul>
        </SectionMessage>
      )}
      {requestError instanceof ApiError && requestError.path && onOpenFile ? (
        <RefusedFile error={requestError} path={requestError.path} onOpenFile={onOpenFile} />
      ) : (
        <ErrorMessage error={requestError} />
      )}
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

/** A refusal that names the file it is about, with the way to that file. */
function RefusedFile({
  error,
  path,
  onOpenFile,
}: {
  error: ApiError;
  path: string;
  onOpenFile: (path: string | null) => void;
}) {
  const hint = errorHint(error);
  return (
    <SectionMessage
      appearance="error"
      isAlert
      actions={[{ text: `Open ${fileName(path)}`, onClick: () => onOpenFile(path) }]}
    >
      <ServerText text={errorText(error)} />
      {hint && (
        <span className="block">
          <ServerText text={hint} />
        </span>
      )}
    </SectionMessage>
  );
}
