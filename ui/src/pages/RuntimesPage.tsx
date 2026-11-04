/**
 * The Runtimes page: the operator-owned substrates a repo's `process.yaml` may name by slug.
 *
 * The same shape as Projects — one full-width list, each row a link to its own page — because
 * a runtime is a thing an operator opens, reads and comes back to, not a row to edit beside a
 * table. The slug is the one column a repo depends on: `process.yaml` writes it after
 * `runtime:`, so it is shown as code, exactly as it must be typed.
 */

import { Link } from "react-router-dom";
import { useRuntimes } from "../api/queries";
import {
  credentialState,
  ISOLATION_LABELS,
  isolationOf,
  RUNTIME_TYPE_LABELS,
} from "../components/runtimes/runtimes";
import {
  Code,
  DataTable,
  EmptyState,
  ErrorMessage,
  IconButton,
  LinkButton,
  Lozenge,
  PageHeader,
  Panel,
  RelativeTime,
  staleSince,
  Tag,
  useCopy,
  usePageTitle,
} from "../components/ui";

const HEAD = [
  { key: "name", content: "Name" },
  { key: "slug", content: "Slug" },
  { key: "type", content: "CLI and authentication" },
  { key: "sandbox", content: "Sandbox" },
  { key: "connected", content: "Connected repositories", align: "end" },
  { key: "credential", content: "Credential" },
  { key: "created", content: "Created" },
] as const;

export function RuntimesPage() {
  const query = useRuntimes();
  usePageTitle(["Runtimes"]);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Runtimes"
        actions={
          <LinkButton
            to="/runtimes/new"
            appearance="primary"
            iconBefore="plus"
            data-assist-anchor="runtimes.new"
          >
            Create runtime
          </LinkButton>
        }
        staleSince={staleSince(query)}
      />

      {query.error && !query.data ? (
        <ErrorMessage
          error={query.error}
          title="Couldn't load the runtimes"
          onRetry={() => query.refetch()}
        />
      ) : (
        <Panel>
          <DataTable
            label="Runtimes"
            head={[...HEAD]}
            rows={query.data}
            isLoading={query.isPending}
            rowKey={(runtime) => runtime.id}
            rowHref={(runtime) => `/runtimes/${runtime.slug}`}
            emptyView={
              <EmptyState
                header="No runtimes yet"
                description="Create one, then name its slug in a repository's process.yaml."
                primaryAction={
                  <LinkButton to="/runtimes/new" appearance="primary">
                    Create runtime
                  </LinkButton>
                }
              />
            }
            cells={(runtime) => {
              const connected =
                credentialState(runtime.type, runtime.credential_names) === "connected";
              return {
                name: (
                  <Link to={`/runtimes/${runtime.slug}`} className="font-medium hover:underline">
                    {runtime.name}
                  </Link>
                ),
                slug: <Slug slug={runtime.slug} />,
                type: <Tag>{RUNTIME_TYPE_LABELS[runtime.type]}</Tag>,
                sandbox: ISOLATION_LABELS[isolationOf(runtime.details)],
                connected: runtime.connected_projects,
                credential: (
                  <Lozenge appearance={connected ? "success" : "neutral"}>
                    {connected ? "Connected" : "None"}
                  </Lozenge>
                ),
                created: <RelativeTime iso={runtime.created_at} />,
              };
            }}
          />
        </Panel>
      )}
    </div>
  );
}

function Slug({ slug }: { slug: string }) {
  const { copied, copy } = useCopy();
  return (
    <span className="inline-flex items-center gap-1">
      <Code>{slug}</Code>
      <IconButton
        icon={copied ? "check" : "copy"}
        label={copied ? "Copied" : `Copy slug ${slug}`}
        spacing="compact"
        onClick={() => copy(slug)}
      />
    </span>
  );
}
