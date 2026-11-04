import { useId, useRef, useState } from "react";
import { useAdoptConfigHead, useUnknownHead } from "../../api/queries";
import type { ConfigAdoption, UnknownHead } from "../../api/types";
import { formatRelative, shortSha } from "../../format";
import { Button, Checkbox, Code, ErrorMessage, Lozenge, ModalDialog } from "../ui";

/**
 * The review behind adopting a `factorial` head no event names (decision 53). Permanent, so a
 * modal (decision 50), and the confirm waits on a tick: the person vouches for commits that an
 * agent may have pushed, and the list below is what they are vouching for. Every string in it
 * came from the push, so it is rendered as text and nothing else.
 */
export function AdoptHeadDialog({
  projectId,
  onClose,
  onAdopted,
}: {
  projectId: number;
  onClose: () => void;
  onAdopted: (result: ConfigAdoption) => void;
}) {
  const head = useUnknownHead(projectId, true);
  const adopt = useAdoptConfigHead(projectId);
  // The tick is for one head: a new head is a new set of commits, so it starts unticked.
  const [readSha, setReadSha] = useState<string>();
  const cancel = useRef<HTMLButtonElement>(null);
  const sha = head.data?.head_sha;
  const read = sha !== undefined && readSha === sha;
  const count = head.data?.total_commits ?? 0;
  const reason = !head.data
    ? head.isPending
      ? "Loading the changes…"
      : "Factorial couldn't read the changes."
    : read
      ? undefined
      : "Tick the box to confirm that you read the changes.";
  // In the body, not beside the button, and always rendered: a line that comes and goes moves
  // the footer with it.
  const reasonId = useId();

  return (
    <ModalDialog
      title={
        head.data
          ? head.data.known_base
            ? `Adopt ${count} ${count === 1 ? "commit" : "commits"} on factorial?`
            : `Adopt factorial head ${shortSha(head.data.head_sha)}?`
          : "Adopt the commits on factorial?"
      }
      appearance="warning"
      width="medium"
      initialFocusRef={cancel}
      onClose={onClose}
      footer={
        <>
          <Button ref={cancel} appearance="subtle" onClick={onClose}>
            Cancel
          </Button>
          <Button
            appearance="warning"
            isLoading={adopt.isPending}
            aria-disabled={reason !== undefined}
            aria-describedby={reason ? reasonId : undefined}
            onClick={() =>
              sha &&
              adopt.mutate(sha, {
                onSuccess: (result) => onAdopted(result),
              })
            }
          >
            Adopt and sync
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <ErrorMessage error={head.error} />
        {head.data && <Review head={head.data} />}
        {head.data && (
          <Checkbox
            label={
              head.data.known_base
                ? "I've read these changes and confirm them"
                : "I confirm this factorial head"
            }
            isChecked={read}
            onChange={(checked) => setReadSha(checked ? sha : undefined)}
          />
        )}
        <p
          id={reasonId}
          aria-hidden={reason ? undefined : true}
          className={`text-body-sm text-subtle${reason ? "" : " invisible"}`}
        >
          {reason ?? "You can adopt now."}
        </p>
        <ErrorMessage error={adopt.error} />
      </div>
    </ModalDialog>
  );
}

function Review({ head }: { head: UnknownHead }) {
  if (!head.known_base) {
    return (
      <>
        <p className="text-body">
          Head <Code>{shortSha(head.head_sha)}</Code> was pushed directly. Factorial has no earlier
          commit on this branch to compare it with. If you adopt it, Factorial records that you
          confirmed this head and syncs it.
        </p>
        <a
          href={head.compare_url}
          target="_blank"
          rel="noreferrer"
          className="font-medium text-brand"
        >
          Open {shortSha(head.head_sha)} on GitHub
        </a>
        <p className="text-body-sm text-subtle">
          You can&apos;t undo this. A later direct push needs its own review.
        </p>
      </>
    );
  }
  return (
    <>
      <p className="text-body">
        These commits didn&apos;t come from Commit and sync or a merged pull request. Factorial
        can&apos;t tell who made them, because agents share the App&apos;s identity and set their
        own author. If you adopt them, Factorial records that you confirmed head{" "}
        <Code>{shortSha(head.head_sha)}</Code> and syncs it.
      </p>
      <section className="space-y-2">
        <h3 className="text-heading-xxs text-subtle uppercase">
          Commits since <span className="normal-case">{shortSha(head.known_base)}</span>, the last
          known commit
        </h3>
        <ul className="divide-y divide-default rounded border border-default">
          {head.commits.map((commit) => (
            <li key={commit.sha} className="flex gap-3 px-3 py-2">
              <Code>{shortSha(commit.sha)}</Code>
              <span className="min-w-0">
                <span className="block break-words">{commit.message.split("\n")[0]}</span>
                <span className="block text-body-sm text-subtle">
                  claimed author {commit.author_name ?? "unknown"}
                  {commit.authored_at ? ` · ${formatRelative(commit.authored_at)}` : ""}
                </span>
              </span>
            </li>
          ))}
        </ul>
        {head.total_commits > head.commits.length && (
          <p className="text-body-sm text-subtle">
            {head.total_commits - head.commits.length} more in the comparison on GitHub.
          </p>
        )}
      </section>
      <section className="space-y-2">
        <h3 className="text-heading-xxs text-subtle uppercase">Files changed</h3>
        <ul className="divide-y divide-default rounded border border-default font-mono text-body-sm">
          {head.files.map((file) => (
            <li key={file.path} className="flex items-center gap-2 px-3 py-2">
              <Lozenge>{file.status}</Lozenge>
              <span className="min-w-0 flex-1 break-all">
                {file.previous_path
                  ? `${file.path} (renamed from ${file.previous_path})`
                  : file.path}
              </span>
              <span className="text-success">+{file.additions}</span>
              <span className="text-danger">&minus;{file.deletions}</span>
            </li>
          ))}
        </ul>
        <a
          href={head.compare_url}
          target="_blank"
          rel="noreferrer"
          className="font-medium text-brand"
        >
          Compare {shortSha(head.known_base)} with {shortSha(head.head_sha)} on GitHub
        </a>
        {head.files_truncated && (
          <p className="text-body-sm text-subtle">
            GitHub lists at most 300 files here. The comparison shows the rest.
          </p>
        )}
      </section>
      <p className="text-body-sm text-subtle">
        You can&apos;t undo this. A later direct push needs its own review. To discard these commits
        instead, an admin resets the branch to {shortSha(head.known_base)}.
      </p>
    </>
  );
}
