/**
 * The two things a reader wants from a ticket's issue: the description it was admitted for, and
 * the issue itself on GitHub.
 *
 * One component for both places that show a ticket — the project page's table rows and the ticket
 * page's header — so the pair cannot drift into two spellings of the same affordance. "View issue"
 * is a real anchor, not a button that navigates: cmd-click and "open in new tab" have to keep
 * working, and `useRowLink` already leaves anchors and buttons alone, so neither swallows a row
 * click into the wrong destination.
 *
 * The description opens in a drawer rather than expanding in place: an issue body has no
 * bounded length, and growing a row would move every row under it (decision 50).
 */

import { useState } from "react";
import { useTicketIssue } from "../../api/queries";
import { type ForgeHost, forgeName, issueHref } from "../../forge";
import { Button, cx, Drawer, ErrorMessage, Icon, IconButton, LinkButton, Spinner } from "../ui";

export function IssueActions({
  ticketId,
  externalKey,
  repo,
  forge,
  size = "sm",
}: {
  ticketId: number;
  externalKey: string;
  /** `owner/name` — the project's repo, the same one every other issue link is built from. */
  repo: string | undefined;
  /** Where the ticket lives. Absent means GitHub, which is what every existing caller linked. */
  forge?: ForgeHost;
  /** `sm` for a table row, where the two are icons with their names as tooltips; `md` for a
   *  page header, where they are short words after their icons, named in full for a screen
   *  reader. */
  size?: "sm" | "md";
}) {
  const [open, setOpen] = useState(false);
  // The issue lives in the project's repo, so with no repo yet there is nowhere for the link to
  // point. The description still works: the server resolves the repo itself from the ticket.
  const href = repo ? issueHref(repo, externalKey, forge) : null;
  const host = forgeName(forge);

  return (
    <span className={cx("inline-flex items-center", size === "md" ? "gap-2" : "gap-1")}>
      {size === "md" ? (
        <>
          <Button
            spacing="compact"
            iconBefore="file-markdown"
            aria-label={`Description of #${externalKey}`}
            onClick={() => setOpen(true)}
          >
            Description
          </Button>
          {href && (
            <LinkButton
              href={href}
              spacing="compact"
              iconBefore="link-external"
              aria-label={`Issue #${externalKey} on ${host}`}
            >
              Issue
            </LinkButton>
          )}
        </>
      ) : (
        <>
          <IconButton
            icon="file-markdown"
            label={`View the description of #${externalKey}`}
            spacing="compact"
            onClick={() => setOpen(true)}
          />
          {href && (
            <a
              href={href}
              target="_blank"
              rel="noreferrer"
              aria-label={`View issue #${externalKey} on ${host}`}
              className="inline-flex size-6 items-center justify-center rounded-md text-icon-subtle hover:bg-neutral-subtle-hovered hover:text-icon"
            >
              <Icon glyph="link-external" />
            </a>
          )}
        </>
      )}
      {open && (
        <IssuePanel
          ticketId={ticketId}
          externalKey={externalKey}
          href={href}
          forge={forge}
          onClose={() => setOpen(false)}
        />
      )}
    </span>
  );
}

/**
 * The issue's description, fetched only while this is mounted (`useTicketIssue`'s `enabled`).
 *
 * The body is rendered as the plain text GitHub stores. It is markdown, and it is left unrendered
 * on purpose: whitespace is preserved, which is most of what a checklist or a fenced block needs
 * to stay readable, and the issue is one click away for the formatted version.
 */
function IssuePanel({
  ticketId,
  externalKey,
  href,
  forge,
  onClose,
}: {
  ticketId: number;
  externalKey: string;
  href: string | null;
  forge?: ForgeHost;
  onClose: () => void;
}) {
  const host = forgeName(forge);
  const { data, error, refetch } = useTicketIssue(ticketId, true);

  return (
    <Drawer
      title={data ? `#${data.number} ${data.title}` : `#${externalKey}`}
      subtitle={data ? `${data.repo} · ${data.state === "open" ? "Open" : "Closed"}` : undefined}
      actions={
        href && (
          <LinkButton href={href} spacing="compact">
            View issue
          </LinkButton>
        )
      }
      onClose={onClose}
    >
      {error ? (
        <ErrorMessage
          error={error}
          title={`Couldn't load the issue from ${host}`}
          onRetry={() => refetch()}
        />
      ) : !data ? (
        <Spinner label={`Loading the issue from ${host}`} />
      ) : data.body ? (
        <p className="text-body leading-relaxed whitespace-pre-wrap text-default">{data.body}</p>
      ) : (
        <p className="text-body text-subtle">This issue has no description.</p>
      )}
    </Drawer>
  );
}
