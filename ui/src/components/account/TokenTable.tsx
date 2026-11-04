/**
 * Every MCP token ever minted, the revoked and expired ones too: rows are never deleted, and a
 * revoked row is the record of who turned it off. Any signed-in person may revoke any token —
 * there are no roles (decision 12). Labels and client strings were typed by someone else, so
 * each is drawn as text, never as a link.
 */

import { type RefObject, useEffect, useState } from "react";
import { useRevokeMcpToken } from "../../api/queries";
import type { McpTokenRow } from "../../api/types";
import { formatAbsolute } from "../../format";
import type { Role } from "../../status";
import {
  Button,
  ConfirmDialog,
  DataTable,
  EmptyState,
  Lozenge,
  Tooltip,
  VisuallyHidden,
} from "../ui";

export const ACCESS_LABELS: Record<McpTokenRow["access"], string> = {
  read: "Read",
  write: "Write",
};

/** `api-token` is the principal a mint with the shared API token records (`api.auth`). */
function ownerName(createdBy: string): string {
  return createdBy === "api-token" ? "API token" : createdBy;
}

export function AbsoluteTime({ iso }: { iso: string }) {
  return (
    <time dateTime={iso} className="whitespace-nowrap">
      {formatAbsolute(iso)}
    </time>
  );
}

const STATES: Record<McpTokenRow["state"], { label: string; role: Role; why?: string }> = {
  active: { label: "Active", role: "success" },
  expired: { label: "Expired", role: "neutral" },
  revoked: { label: "Revoked", role: "danger" },
  rotated: {
    label: "No longer valid",
    role: "warning",
    why: "The server's signing key changed. Create a new token.",
  },
};

function TokenState({ state }: { state: McpTokenRow["state"] }) {
  const { label, role, why } = STATES[state];
  const lozenge = <Lozenge appearance={role}>{label}</Lozenge>;
  if (!why) return lozenge;
  return (
    <Tooltip content={why} describes>
      {/* biome-ignore lint/a11y/noNoninteractiveTabindex: a tooltip's trigger has to be reachable by keyboard */}
      <span tabIndex={0} className="inline-flex rounded-xs">
        {lozenge}
      </span>
    </Tooltip>
  );
}

const HEAD = [
  { key: "token", content: "Token" },
  { key: "access", content: "Access" },
  { key: "created", content: "Created" },
  { key: "expires", content: "Expires" },
  { key: "lastUsed", content: "Last used" },
  { key: "state", content: "Status" },
  { key: "actions", content: <VisuallyHidden>Actions</VisuallyHidden> },
] as const;

export function TokenTable({
  tokens,
  headingRef,
}: {
  tokens: McpTokenRow[];
  /** Where the focus goes after a revoke, since the button that had it is gone. */
  headingRef: RefObject<HTMLElement | null>;
}) {
  const revoke = useRevokeMcpToken();
  const [revoking, setRevoking] = useState<McpTokenRow | null>(null);
  const [revoked, setRevoked] = useState(false);

  // An effect, not the success callback: the dialog's layer keeps the page inert until it has
  // unmounted, and an inert heading cannot take the focus.
  useEffect(() => {
    if (!revoked) return;
    setRevoked(false);
    headingRef.current?.focus();
  }, [revoked, headingRef]);

  return (
    <>
      <DataTable
        label="MCP tokens"
        head={[...HEAD]}
        rows={tokens}
        rowKey={(row) => row.id}
        emptyView={
          <EmptyState
            headingLevel={3}
            header="No tokens yet"
            description="Select New token to create one."
          />
        }
        cells={(row) => ({
          token: (
            <span className="block">
              <Tooltip content={row.label} onlyIfTruncated>
                <span className="block max-w-48 truncate font-medium">{row.label}</span>
              </Tooltip>
              <span className="block text-body-sm text-subtle">by {ownerName(row.created_by)}</span>
            </span>
          ),
          access: (
            <Lozenge appearance={row.access === "write" ? "information" : "neutral"}>
              {ACCESS_LABELS[row.access]}
            </Lozenge>
          ),
          created: <AbsoluteTime iso={row.created_at} />,
          expires: <AbsoluteTime iso={row.expires_at} />,
          lastUsed: row.last_used_at ? (
            <span className="block">
              <AbsoluteTime iso={row.last_used_at} />
              {(row.last_used_client || row.last_used_ip) && (
                <span className="block text-body-sm text-subtle">
                  {[row.last_used_client, row.last_used_ip].filter(Boolean).join(" · ")}
                </span>
              )}
            </span>
          ) : (
            <span className="text-subtle">Never</span>
          ),
          state: <TokenState state={row.state} />,
          actions: row.state === "active" && (
            <Button
              spacing="compact"
              aria-label={`Revoke ${row.label}`}
              onClick={() => setRevoking(row)}
            >
              Revoke
            </Button>
          ),
        })}
      />
      {revoking && (
        <ConfirmDialog
          title={`Revoke token "${revoking.label}"?`}
          consequence={`Created by ${ownerName(revoking.created_by)}. Clients using it lose access at their next call. You can't undo this.`}
          confirmLabel="Revoke token"
          isPending={revoke.isPending}
          error={revoke.error}
          onCancel={() => {
            revoke.reset();
            setRevoking(null);
          }}
          onConfirm={() =>
            revoke.mutate(revoking.id, {
              onSuccess: () => {
                setRevoking(null);
                setRevoked(true);
              },
            })
          }
        />
      )}
    </>
  );
}
