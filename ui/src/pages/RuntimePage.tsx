/**
 * One runtime, by slug — the page a row on `/runtimes` opens. The slug is in the URL rather
 * than the id because it is the name a repo writes, so a pasted link reads.
 */

import { useNavigate, useParams } from "react-router-dom";
import { useRuntimes } from "../api/queries";
import { RuntimeActions, RuntimeDetail } from "../components/runtimes/RuntimeDetail";
import {
  credentialState,
  RUNTIME_TYPE_LABELS,
  requiresStoredCredential,
} from "../components/runtimes/runtimes";
import {
  Code,
  EmptyState,
  ErrorMessage,
  LinkButton,
  Lozenge,
  PageHeader,
  SkeletonRows,
  staleSince,
  Tag,
  usePageTitle,
} from "../components/ui";

export function RuntimePage() {
  const { slug } = useParams();
  const navigate = useNavigate();
  const query = useRuntimes();
  const runtime = query.data?.find((row) => row.slug === slug) ?? null;
  usePageTitle([runtime?.name ?? slug, "Runtimes"]);

  if (query.data === undefined) {
    return (
      <div className="space-y-6">
        <PageHeader title={slug} isLoading={query.isPending} />
        {query.error ? (
          <ErrorMessage
            error={query.error}
            title="Couldn't load this runtime"
            onRetry={() => query.refetch()}
          />
        ) : (
          <SkeletonRows rows={4} columns={2} />
        )}
      </div>
    );
  }

  if (runtime === null) {
    return (
      <div className="space-y-6">
        <PageHeader title="Runtime not found" />
        <EmptyState
          header="Factorial can't find that runtime"
          description={
            <>
              No runtime has the slug <Code>{slug}</Code>. It may have been deleted.
            </>
          }
          primaryAction={<LinkButton to="/runtimes">Back to runtimes</LinkButton>}
        />
      </div>
    );
  }

  const connected = credentialState(runtime.type, runtime.credential_names) === "connected";
  return (
    <div className="space-y-6">
      <PageHeader
        title={runtime.name}
        actions={<RuntimeActions runtime={runtime} onDeleted={() => navigate("/runtimes")} />}
        bottomBar={
          <>
            <Tag>{RUNTIME_TYPE_LABELS[runtime.type]}</Tag>
            <Lozenge appearance={connected ? "success" : "neutral"}>
              {connected
                ? requiresStoredCredential(runtime.type)
                  ? "Credential connected"
                  : "Host role"
                : "No credential"}
            </Lozenge>
            <Code>{runtime.slug}</Code>
          </>
        }
        staleSince={staleSince(query)}
      />
      {/* Remount on a different runtime, so the name field starts from that row's name. */}
      <RuntimeDetail key={runtime.id} runtime={runtime} />
    </div>
  );
}
