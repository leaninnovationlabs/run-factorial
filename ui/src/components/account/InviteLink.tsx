/**
 * An invite's link, as the answer that creates it shows it: once, with a way to copy it. Both a
 * new invite (`/members/invite`) and a new link for a pending one (the Members page) end here.
 */

import type { InviteCreated } from "../../api/types";
import { CopyButton } from "../ui";

/** The address an invite's link is shared as: the API's own URL, or its path on this origin. */
export function inviteLink(invite: InviteCreated, origin = window.location.origin): string {
  return invite.url ?? `${origin}${invite.path}`;
}

export function InviteLink({ invite }: { invite: InviteCreated }) {
  const link = inviteLink(invite);
  return (
    <div className="space-y-4">
      <p className="text-body text-subtle">
        Send this link to {invite.email}. It works once, for that email only, until it expires. It
        is not shown again.
      </p>
      <div className="flex items-center gap-2">
        <code className="min-w-0 flex-1 truncate">{link}</code>
        <CopyButton value={link} name="invite link" />
      </div>
    </div>
  );
}
