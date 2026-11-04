/**
 * A write the assistant proposed, held until its conversation's owner decides it
 * (`assistant/proposals.py`). The card says what would happen and to what; Approve runs it once,
 * as the person, through the service the dashboard's own button calls, and Decline drops it.
 *
 * The card's own row is redrawn as the proposal moves on, so the stream brings its outcome; the
 * answer to a press is shown at once, ahead of that. A service that refused says why, in its own
 * words, as it would anywhere else. A press is sent once, whatever happens to the button.
 */

import { type ReactNode, useRef } from "react";
import { useApproveProposal, useDeclineProposal } from "../../../api/queries";
import type { ProposalPayload, ProposalStatus } from "../../../api/types";
import type { Role } from "../../../status";
import { ActionError, Button, CodeBlock, Lozenge, RelativeTime } from "../../ui";
import { DashboardLink } from "../AssistantProvider";

const STATUS: Record<ProposalStatus, { label: string; role: Role }> = {
  pending: { label: "Waiting for you", role: "warning" },
  approved: { label: "Approved", role: "information" },
  succeeded: { label: "Succeeded", role: "success" },
  refused: { label: "Refused", role: "danger" },
  declined: { label: "Declined", role: "neutral" },
  expired: { label: "Expired", role: "neutral" },
};

/** The preview's facts, in the order a reader checks them, with their words. */
const FACTS: [key: string, label: string][] = [
  ["title", "Issue"],
  ["step", "Step"],
  ["status", "Status"],
  ["attempt", "Attempt"],
  ["answer", "Answer"],
  ["project", "Project"],
  ["repo", "Repository"],
  ["assignee", "Assignee"],
  ["process", "Process"],
];

function text(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number") return String(value);
  return null;
}

/** A run as `RestartRunOut` gives it, in words: "build attempt 2". */
function runOf(value: unknown): string | null {
  if (typeof value !== "object" || value === null) return null;
  const { step, attempt } = value as { step?: unknown; attempt?: unknown };
  const name = text(step);
  return name && typeof attempt === "number" ? `${name} attempt ${attempt}` : null;
}

/** Pull requests as the restart's preview gives them (`RestartPrOut`): the readable ones. */
function prLinks(value: unknown): { number: string; url: string }[] {
  return (Array.isArray(value) ? value : []).flatMap((pr) => {
    const number = text((pr as { number?: unknown })?.number);
    const url = text((pr as { url?: unknown })?.url);
    return number && url ? [{ number, url }] : [];
  });
}

function PrLinks({ title, prs }: { title: string; prs: { number: string; url: string }[] }) {
  return (
    <div>
      <p className="text-body-sm text-subtle">{title}</p>
      <ul className="text-body-sm">
        {prs.map((pr) => (
          <li key={pr.number}>
            <a
              href={pr.url}
              target="_blank"
              rel="noreferrer"
              className="font-medium hover:underline"
            >
              #{pr.number}
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Preview({ preview }: { preview: Record<string, unknown> }) {
  const ticket = text(preview.ticket);
  const ticketId = typeof preview.ticket_id === "number" ? preview.ticket_id : null;
  const files = Array.isArray(preview.would_write) ? preview.would_write : [];
  const diff = text(preview.diff);
  const body = text(preview.body);
  const rows: [string, ReactNode][] = [];
  if (ticket) {
    rows.push([
      "Ticket",
      ticketId === null ? (
        ticket
      ) : (
        <DashboardLink key="ticket" to={`/tickets/${ticketId}`}>
          {ticket}
        </DashboardLink>
      ),
    ]);
  }
  for (const [key, label] of FACTS) {
    const value = text(preview[key]);
    if (value) rows.push([label, value]);
  }
  // A restart's precheck adds what the approval removes, as the dashboard's dialog lists it.
  const live = runOf(preview.live_run);
  if (live) rows.push(["Cancels", live]);
  const parked = runOf(preview.parked_run);
  if (parked) rows.push(["Abandons", parked]);
  const prs = prLinks(preview.close_prs);
  const merged = prLinks(preview.merged_outside);
  const branches = (Array.isArray(preview.delete_branches) ? preview.delete_branches : []).flatMap(
    (branch) => text(branch) ?? [],
  );
  return (
    <div className="space-y-2">
      {rows.length > 0 && (
        <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 text-body">
          {rows.map(([label, value]) => (
            <div key={label} className="contents">
              <dt className="text-subtle">{label}</dt>
              <dd className="min-w-0 break-words text-default">{value}</dd>
            </div>
          ))}
        </dl>
      )}
      {body && (
        <p className="max-h-40 overflow-y-auto rounded-md bg-neutral p-2 text-body whitespace-pre-wrap">
          {body}
        </p>
      )}
      {files.length > 0 && (
        <div>
          <p className="text-body-sm text-subtle">Files it writes</p>
          <ul className="font-mono text-body-sm">
            {files.map((file) => {
              const path = text((file as { path?: unknown })?.path);
              return path ? <li key={path}>{path}</li> : null;
            })}
          </ul>
        </div>
      )}
      {prs.length > 0 && <PrLinks title="Pull requests it closes" prs={prs} />}
      {merged.length > 0 && (
        <PrLinks title="Already merged: the restart may repeat this work" prs={merged} />
      )}
      {branches.length > 0 && (
        <div>
          <p className="text-body-sm text-subtle">Branches it deletes</p>
          <ul className="font-mono text-body-sm">
            {branches.map((branch) => (
              <li key={branch}>{branch}</li>
            ))}
          </ul>
        </div>
      )}
      {diff && (
        <CodeBlock text={diff} label="The change" isCopyable={false} className="[&>pre]:max-h-80" />
      )}
    </div>
  );
}

function errorOf(result: Record<string, unknown> | null): string | null {
  return text(result?.error);
}

export function ProposalCard({ payload }: { payload: ProposalPayload }) {
  const approve = useApproveProposal();
  const decline = useDeclineProposal();
  const pressed = useRef(false);
  const answer = approve.data ?? decline.data;
  const status = answer?.status ?? payload.status;
  const result = answer?.result ?? payload.result;
  const facts = STATUS[status] ?? STATUS.pending;
  const busy = approve.isPending || decline.isPending;
  const reason = status === "refused" || status === "approved" ? errorOf(result) : null;

  function decide(which: "approve" | "decline") {
    if (pressed.current) return;
    pressed.current = true;
    const mutation = which === "approve" ? approve : decline;
    mutation.mutate(payload.proposal_id, {
      // A refusal of the press itself (an expired or decided proposal) leaves nothing pending, so
      // the card may be pressed again once it has said why.
      onError: () => {
        pressed.current = false;
      },
    });
  }

  return (
    <section
      aria-label={`Proposal: ${payload.title}`}
      className="space-y-3 rounded-lg border border-default bg-surface p-3"
    >
      <header className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-heading-xs text-default">{payload.title}</h3>
        <Lozenge appearance={facts.role}>{facts.label}</Lozenge>
      </header>
      <Preview preview={payload.preview ?? {}} />
      {reason && <p className="text-body whitespace-pre-wrap text-danger">{reason}</p>}
      <ActionError error={approve.error ?? decline.error} />
      {status === "pending" && !answer && (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            appearance="primary"
            isLoading={approve.isPending}
            aria-disabled={busy || undefined}
            onClick={() => decide("approve")}
          >
            Approve
          </Button>
          <Button
            appearance="subtle"
            isLoading={decline.isPending}
            aria-disabled={busy || undefined}
            onClick={() => decide("decline")}
          >
            Decline
          </Button>
          <span className="text-body-sm text-subtle">
            Expires <RelativeTime iso={payload.expires_at} />
          </span>
        </div>
      )}
    </section>
  );
}
