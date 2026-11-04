import type { PendingIssue } from "../../api/types";
import { type ForgeHost, issueHref } from "../../forge";
import { NO_VALUE } from "../../format";
import { Code, DataTable, EmptyState, Lozenge, Tag } from "../ui";

const HEAD = [
  { key: "issue", content: "Issue" },
  { key: "process", content: "Process" },
  { key: "awaiting", content: "Awaiting" },
] as const;

/**
 * The open issues that are not tickets yet, under the Tickets table's `Admission` filter.
 *
 * Recomputed on every read rather than played back from the event log: a refusal has to say
 * whether it is STILL true, and an append-only row can only say that it happened once.
 *
 * Two kinds of row, one column. An issue whose lane was resolved is waiting on that lane's
 * `entry.when`, and the column lists the leaves that are still false — "its entry condition is
 * not satisfied" sends a reader to the config, whereas "the factorial:new label on the issue"
 * tells them what to do to the issue. The API words them (`needs`); one from before it did sends
 * the rule code alone (`awaiting`), which still says it. An issue whose lane never resolved is
 * misconfigured, is waiting for nothing, and says so in a warning lozenge; that case used to be a
 * banner above the table, which separated the verdict from the issue it was about.
 */
export function AdmissionTable({
  issues,
  repo,
  forge,
  isLoading,
}: {
  issues: PendingIssue[] | undefined;
  repo: string;
  forge?: ForgeHost;
  isLoading: boolean;
}) {
  return (
    <DataTable<PendingIssue, (typeof HEAD)[number]["key"]>
      label="Issues awaiting admission"
      head={[...HEAD]}
      rows={issues}
      isLoading={isLoading}
      rowKey={(issue) => issue.external_key}
      isHeadSticky
      emptyView={
        <EmptyState
          header="Every open issue has been admitted"
          description="An issue appears here while it waits for its process's entry condition, or while something stops it from being admitted at all."
        />
      }
      cells={(issue) => ({
        // A plain link to GitHub, not the ticket page's `IssueActions`: there is no ticket, so
        // there is nothing for the description panel — which reads through a ticket — to fetch.
        issue: (
          <div className="min-w-0">
            <a
              href={issueHref(repo, issue.external_key, forge)}
              target="_blank"
              rel="noreferrer"
              className="font-medium hover:underline"
            >
              #{issue.external_key}
            </a>
            {issue.title && (
              <p className="line-clamp-2 max-w-md text-body-sm text-subtle">{issue.title}</p>
            )}
          </div>
        ),
        process: issue.process_key ? <Tag>{issue.process_key}</Tag> : <span>{NO_VALUE}</span>,
        awaiting: <Awaiting issue={issue} />,
      })}
    />
  );
}

function Awaiting({ issue }: { issue: PendingIssue }) {
  if (issue.reason !== "not_ready") {
    return (
      <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1">
        <Lozenge appearance="warning" iconBefore="exclamation-circle">
          Can't be admitted
        </Lozenge>
        <span className="text-body-sm text-subtle">{issue.detail}</span>
      </div>
    );
  }
  // An entry condition that is false with no false leaf beneath it is possible in principle
  // (`failures()` falls back to the node's own line), so the sentence stays as the backstop.
  if (issue.needs?.length) {
    return <span className="text-body-sm">{`Waiting for: ${issue.needs.join("; ")}`}</span>;
  }
  if (issue.awaiting.length === 0) {
    return <span className="text-body-sm text-subtle">{issue.detail}</span>;
  }
  return (
    <ul className="space-y-0.5">
      {issue.awaiting.map((line) => (
        <li key={line}>
          <Code>{line}</Code>
        </li>
      ))}
    </ul>
  );
}
