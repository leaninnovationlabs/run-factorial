/**
 * One runtime, editable: its details, its credentials, its metadata, and what connects to it.
 *
 * Each section is its own form with its own error, as the project page's bodies are: renaming,
 * storing a bundle and starting a device login fail independently, and each failure belongs
 * beside its own button. The credentials section is `RuntimeCredentials.tsx`.
 */

import { useState } from "react";
import { Link } from "react-router-dom";
import {
  useDeleteRuntime,
  useRuntimeOptions,
  useRuntimeProjects,
  useUpdateRuntime,
} from "../../api/queries";
import type { Runtime } from "../../api/types";
import { forgeHost, repoHref } from "../../forge";
import { formatAbsolute } from "../../format";
import {
  Button,
  Code,
  ConfirmDialog,
  DropdownMenu,
  EmptyState,
  ErrorMessage,
  Icon,
  InlineEditText,
  Lozenge,
  Panel,
  SkeletonRows,
  showFlag,
  TextField,
} from "../ui";
import { Credentials } from "./RuntimeCredentials";
import { RuntimeMetadata } from "./RuntimeMetadata";
import { axesOf, axisLabels, offerKey } from "./runtimeChoice";
import { bedrockModelOf, bedrockRegionOf, isolationOf, sandboxLabelOf } from "./runtimes";

export function RuntimeDetail({ runtime }: { runtime: Runtime }) {
  return (
    <div className="space-y-4">
      <Panel title="Details">
        <Details runtime={runtime} />
      </Panel>
      <Panel title="Credentials">
        <Credentials runtime={runtime} />
      </Panel>
      <Panel title="Metadata">
        <RuntimeMetadata runtime={runtime} />
      </Panel>
      <ConnectedRepositories runtime={runtime} />
    </div>
  );
}

/**
 * The page's own actions, in its header: a More menu holding the one that cannot be undone, which
 * asks in a dialog before it deletes (decision 50).
 */
export function RuntimeActions({
  runtime,
  onDeleted,
}: {
  runtime: Runtime;
  onDeleted: () => void;
}) {
  const remove = useDeleteRuntime(runtime.id);
  const [confirming, setConfirming] = useState(false);
  return (
    <>
      <DropdownMenu
        trigger={{ label: "More actions", icon: "more", iconOnly: true }}
        items={[{ label: "Delete runtime", onClick: () => setConfirming(true) }]}
      />
      {confirming && (
        <ConfirmDialog
          title={`Delete runtime ${runtime.slug}?`}
          consequence={`This removes ${runtime.slug} and its stored credentials. Finished runs keep their history. Processes that use this runtime can't start new runs until they switch to another.`}
          confirmLabel="Delete runtime"
          isPending={remove.isPending}
          error={remove.error}
          onCancel={() => {
            remove.reset();
            setConfirming(false);
          }}
          onConfirm={() =>
            remove.mutate(undefined, {
              onSuccess: () => {
                showFlag({ appearance: "success", title: `Deleted runtime ${runtime.slug}` });
                onDeleted();
              },
            })
          }
        />
      )}
    </>
  );
}

/** The projects whose process names this runtime — the rows the list's count stands for.
 *  Read-only: the binding is written by a sync of the repo's `process.yaml`, never here. */
function ConnectedRepositories({ runtime }: { runtime: Runtime }) {
  const { data: projects, isPending, error, refetch } = useRuntimeProjects(runtime.id);
  return (
    <Panel title="Connected repositories" count={projects?.length}>
      <div className="p-4">
        {error && !projects ? (
          <ErrorMessage
            error={error}
            title="Couldn't load the connected repositories"
            onRetry={() => refetch()}
          />
        ) : isPending ? (
          <SkeletonRows rows={2} columns={2} />
        ) : !projects?.length ? (
          <EmptyState
            headingLevel={3}
            header="No process names this runtime yet"
            description={
              <>
                A process uses it once its <Code>process.yaml</Code> says{" "}
                <Code>runtime: {runtime.slug}</Code> and the project syncs.
              </>
            }
          />
        ) : (
          <ul className="divide-y divide-default rounded-md border border-default">
            {projects.map((project) => (
              <li
                key={project.id}
                className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 px-3 py-2 text-body"
              >
                <Link to={`/projects/${project.id}`} className="font-medium hover:underline">
                  {project.name}
                </Link>
                <a
                  href={repoHref(project.github_repo, forgeHost(null, undefined, project.forge))}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 font-mono text-body-sm text-subtle hover:text-default hover:underline"
                >
                  {project.github_repo}
                  <Icon glyph="link-external" size="small" />
                </a>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Panel>
  );
}

function Details({ runtime }: { runtime: Runtime }) {
  const update = useUpdateRuntime(runtime.id);
  const options = useRuntimeOptions();
  const labels = axisLabels(axesOf(runtime.type, runtime.details));
  const isolation = isolationOf(runtime.details);
  return (
    <div className="grid gap-4 p-4 sm:grid-cols-2">
      <InlineEditText
        label="Name"
        value={runtime.name}
        validate={(value) => (value.trim() === "" ? "A runtime needs a name." : null)}
        isLoading={update.isPending && update.variables?.name !== undefined}
        error={update.variables?.name !== undefined ? update.error : null}
        onConfirm={(name) => update.mutate({ name: name.trim() })}
      />
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-body">
        <dt className="text-subtle">Slug</dt>
        <dd>
          <Code>{runtime.slug}</Code>
        </dd>
        <dt className="text-subtle">CLI</dt>
        <dd className="text-default">{labels.cli}</dd>
        <dt className="text-subtle">Authentication</dt>
        <dd className="text-default">{labels.auth}</dd>
        <dt className="text-subtle">Sandbox</dt>
        <dd className="flex flex-wrap items-center gap-2 text-default">
          {sandboxLabelOf(runtime)}
          {options.data && !options.data.offered.includes(offerKey(runtime.type, isolation)) && (
            <Lozenge>No longer offered</Lozenge>
          )}
        </dd>
        <dt className="text-subtle">Created</dt>
        <dd className="text-default">
          <time dateTime={runtime.created_at}>{formatAbsolute(runtime.created_at)}</time>
        </dd>
      </dl>
      {runtime.type === "codex_bedrock" && <BedrockEndpoint runtime={runtime} />}
    </div>
  );
}

function BedrockEndpoint({ runtime }: { runtime: Runtime }) {
  const update = useUpdateRuntime(runtime.id);
  const [region, setRegion] = useState(bedrockRegionOf(runtime.details));
  const [model, setModel] = useState(bedrockModelOf(runtime.details));
  return (
    <div className="space-y-2 border-t border-default pt-4 sm:col-span-2">
      <p className="text-body-sm text-subtle">
        Bedrock Runtime endpoint. GovCloud uses us-gov-west-1 and us-gov.xai.grok-4.6; commercial
        uses a standard region and us.xai.grok-4.6.
      </p>
      <div className="grid gap-3 sm:grid-cols-2">
        <TextField
          label="Region"
          value={region}
          onChange={(event) => setRegion(event.target.value)}
          isMonospaced
        />
        <TextField
          label="Model"
          value={model}
          onChange={(event) => setModel(event.target.value)}
          isMonospaced
        />
      </div>
      <ErrorMessage error={update.error} />
      <Button
        spacing="compact"
        isLoading={update.isPending}
        onClick={() =>
          update.mutate(
            { details: { ...runtime.details, region: region.trim(), model: model.trim() } },
            {
              onSuccess: () => showFlag({ appearance: "success", title: "Saved Bedrock endpoint" }),
            },
          )
        }
      >
        Save endpoint
      </Button>
    </div>
  );
}
