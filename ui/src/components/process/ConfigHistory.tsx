import { useState } from "react";
import { useProcessVersions, useRestoreConfig, useRestorePreview } from "../../api/queries";
import type { ProjectDetail, RestorePreview } from "../../api/types";
import { shortSha } from "../../format";
import { asSentence } from "../activity/words";
import {
  Button,
  Code,
  ConfirmDialog,
  Drawer,
  EmptyState,
  ErrorMessage,
  Lozenge,
  QueryBoundary,
  RelativeTime,
  SkeletonRows,
  showFlag,
} from "../ui";
import { type SyncedCommit, syncedCommits } from "./syncedCommits";

const files = (n: number) => `${n} ${n === 1 ? "file" : "files"}`;

/** What a restore does, in the words its confirmation carries. */
function consequence(preview: RestorePreview): string {
  const retires = preview.retires.length > 0 ? ` Retires ${preview.retires.join(", ")}.` : "";
  return (
    `Rewrites ${files(preview.writes.length)} and deletes ${preview.deletes.length} under ` +
    `.factorial/, in one new commit on the factorial branch, then syncs it.${retires} ` +
    "Tickets already running keep the version they were admitted on."
  );
}

/**
 * Every commit Factorial has synced, and the way back to one (tech-stack decision 93). A restore
 * is a new commit that puts the whole config folder back, so it is confirmed with what it will
 * write, delete and retire, read from the server against the branch head as it is now.
 */
export function ConfigHistory({
  project,
  onClose,
}: {
  project: ProjectDetail;
  onClose: () => void;
}) {
  const versions = useProcessVersions(project.id, true);
  /** The commit whose Restore was pressed: its preview is being read, or its confirmation is up. */
  const [target, setTarget] = useState<SyncedCommit | null>(null);
  const preview = useRestorePreview(project.id, target?.versionId ?? null);
  const restore = useRestoreConfig(project.id);

  function restoreNow(to: SyncedCommit, plan: RestorePreview) {
    restore.mutate(
      { versionId: to.versionId, expectedHead: plan.head_sha },
      {
        onSuccess: (result) => {
          const synced = result.sync_error === null;
          showFlag({
            appearance: synced ? "success" : "warning",
            title: `Restored the config to ${shortSha(to.sha)}`,
            description: synced ? "New tickets run it." : asSentence(result.sync_error ?? ""),
          });
          onClose();
        },
      },
    );
  }

  return (
    <Drawer
      title="Config history"
      subtitle="Every commit Factorial has synced from the factorial branch."
      onClose={onClose}
    >
      <ErrorMessage error={target ? preview.error : null} title="Couldn't prepare the restore" />
      <QueryBoundary
        query={versions}
        loading={<SkeletonRows rows={4} columns={3} />}
        errorTitle="Couldn't load the history"
      >
        {(rows) => {
          const commits = syncedCommits(rows, project.github_repo);
          if (commits.length === 0) {
            return <EmptyState header="Nothing is synced yet" headingLevel={3} />;
          }
          return (
            <ul className="divide-y divide-default">
              {commits.map((commit) => {
                const chosen = target?.sha === commit.sha;
                return (
                  <li key={commit.sha} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-3">
                    <Code>{shortSha(commit.sha)}</Code>
                    <span className="min-w-0 text-body text-default">
                      {commit.processes.join(", ")}
                    </span>
                    <span className="text-body-sm text-subtle">
                      synced <RelativeTime iso={commit.syncedAt} />
                      {commit.adoptedBy && ` · adopted by ${commit.adoptedBy}`}
                    </span>
                    <span className="ml-auto flex items-center gap-2">
                      {chosen && preview.data?.unchanged && (
                        <span className="text-body-sm text-subtle">
                          The branch already holds this version
                        </span>
                      )}
                      {commit.current ? (
                        <Lozenge isQuiet>Current</Lozenge>
                      ) : (
                        <Button
                          spacing="compact"
                          isLoading={chosen && preview.isFetching}
                          onClick={() => {
                            restore.reset();
                            setTarget(commit);
                          }}
                        >
                          Restore
                        </Button>
                      )}
                    </span>
                  </li>
                );
              })}
            </ul>
          );
        }}
      </QueryBoundary>
      {target && preview.data && !preview.data.unchanged && (
        <ConfirmDialog
          title={`Restore the config to ${shortSha(target.sha)}?`}
          consequence={consequence(preview.data)}
          confirmLabel="Restore"
          appearance="warning"
          isPending={restore.isPending}
          error={restore.error}
          onConfirm={() => restoreNow(target, preview.data)}
          onCancel={() => setTarget(null)}
        />
      )}
    </Drawer>
  );
}
