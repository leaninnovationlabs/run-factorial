/**
 * Members: who may act in this organization and as which role, and the invites still waiting
 * (design.md §7.19), in every deployment; the side navigation offers it to a reader who may invite.
 *
 * The same shape as Runtimes: one full-width list, and inviting is its own page
 * (`/members/invite`) that the header's action opens and that returns here. The members and the
 * pending invites are two tabs of that one list, the shown one kept in `?view=` so the invite page
 * can return to the invites it just added to. Every action is
 * offered only to a reader holding its permission, and the API decides anyway. A refusal — the
 * last owner leaving, say — is said where it was asked: in the dialog that confirms a removal,
 * in a flag for a role change or a revoke, never as a banner the list carries afterwards.
 *
 * Under the list, folded, every reader can see what each role may do: the API's own roles and
 * permission catalogue as a grid, so it cannot drift from what the API enforces.
 */

import { useState } from "react";
import {
  useCan,
  useChangeMemberRole,
  useCreateInvite,
  useInvites,
  useMembers,
  useOrganizationDetail,
  usePermissions,
  useRemoveMember,
  useRevokeInvite,
  useRoles,
} from "../api/organizationQueries";
import type { Invite, Member, Permission, Role } from "../api/types";
import { useAuth } from "../auth";
import { InviteLink } from "../components/account/InviteLink";
import {
  Button,
  ConfirmDialog,
  DataTable,
  EmptyState,
  ErrorMessage,
  Icon,
  LinkButton,
  Lozenge,
  ModalDialog,
  PageHeader,
  Panel,
  QueryBoundary,
  RelativeTime,
  Select,
  showFlag,
  staleSince,
  Tab,
  TabList,
  TabPanel,
  Tabs,
  usePageTitle,
  VisuallyHidden,
} from "../components/ui";
import { NO_VALUE } from "../format";
import { orgless } from "../org";
import { useViewParams } from "../searchParams";

export const INVITES_VIEW = "invites";

/** The roles a reader may grant: those holding nothing the reader does not hold. */
export function grantable(roles: Role[], held: string[]): Role[] {
  return roles.filter((role) => role.permissions.every((key) => held.includes(key)));
}

/** The catalogue's keys, each with whether every role holds it, in the API's order of roles. */
export function roleGrid(permissions: Permission[], roles: Role[]) {
  return permissions.map((permission) => ({
    ...permission,
    held: Object.fromEntries(
      roles.map((role) => [role.key, role.permissions.includes(permission.key)]),
    ),
  }));
}

/** Who sent an invite: their name while they are a member, else the address they sent it from.
 *  `api-token` is the principal a call on the shared API token records (`api.auth`). */
export function inviterName(createdBy: string, members: Member[]): string {
  if (createdBy === "api-token") return "API token";
  return members.find((member) => member.email === createdBy)?.name ?? createdBy;
}

function refused(title: string) {
  return (error: unknown) =>
    showFlag({
      appearance: "error",
      title,
      description: error instanceof Error ? error.message : undefined,
    });
}

export default function MembersPage() {
  usePageTitle(["Members"]);
  const canInvite = useCan("members:invite");
  const members = useMembers();
  const invites = useInvites(canInvite);
  const [params, setParams] = useViewParams();
  const view = canInvite && params.get("view") === INVITES_VIEW ? INVITES_VIEW : "members";
  const memberList =
    members.error && !members.data ? (
      <ErrorMessage
        error={members.error}
        title="Couldn't load the members"
        onRetry={() => members.refetch()}
      />
    ) : (
      <MemberTable rows={members.data} isLoading={members.isPending} />
    );
  return (
    <div className="space-y-6">
      <PageHeader
        title="Members"
        actions={
          canInvite && (
            <LinkButton to="/members/invite" appearance="primary" iconBefore="plus">
              Invite people
            </LinkButton>
          )
        }
        staleSince={staleSince(members)}
      />
      {canInvite ? (
        <Tabs
          id="members-view"
          selected={view}
          onChange={(next) => setParams({ view: next === INVITES_VIEW ? next : null })}
        >
          {/* One child of the page's stack, so its spacing does not add to the panel's own. */}
          <div>
            <TabList label="Members and invites">
              <Tab value="members" badge={members.data?.length}>
                Members
              </Tab>
              <Tab value={INVITES_VIEW} badge={invites.data?.length}>
                Pending invites
              </Tab>
            </TabList>
            <TabPanel value="members">{memberList}</TabPanel>
            <TabPanel value={INVITES_VIEW}>
              {invites.error && !invites.data ? (
                <ErrorMessage
                  error={invites.error}
                  title="Couldn't load the invites"
                  onRetry={() => invites.refetch()}
                />
              ) : (
                <InviteTable rows={invites.data} isLoading={invites.isPending} />
              )}
            </TabPanel>
          </div>
        </Tabs>
      ) : (
        memberList
      )}
      <RoleGuide />
    </div>
  );
}

function heldMark(isHeld: boolean | undefined) {
  return isHeld ? (
    <Icon glyph="check" label="Allowed" className="text-icon-success" />
  ) : (
    <span className="text-subtlest">
      <span aria-hidden>{NO_VALUE}</span>
      <VisuallyHidden>Not allowed</VisuallyHidden>
    </span>
  );
}

function RoleGuide() {
  const [isOpen, setOpen] = useState(false);
  const roles = useRoles();
  const permissions = usePermissions();
  return (
    <Panel
      title="What each role can do"
      isCollapsible
      isOpen={isOpen}
      onToggle={() => setOpen((open) => !open)}
    >
      <QueryBoundary query={roles} errorTitle="Couldn't load the roles">
        {(roleList) => (
          <QueryBoundary query={permissions} errorTitle="Couldn't load the permissions">
            {(permissionList) => (
              <div className="space-y-4 py-4">
                <dl className="space-y-1 px-4 text-body">
                  {roleList.map((role) => (
                    <div key={role.key} className="flex flex-wrap gap-x-2">
                      <dt className="font-semibold">{role.name}</dt>
                      <dd className="text-subtle">{role.description}</dd>
                    </div>
                  ))}
                </dl>
                <DataTable
                  label="Permissions by role"
                  head={[
                    { key: "permission", content: "Permission" },
                    ...roleList.map((role) => ({ key: role.key, content: role.name })),
                  ]}
                  rows={roleGrid(permissionList, roleList)}
                  rowKey={(row) => row.key}
                  emptyView={<EmptyState header="No permissions" />}
                  cells={(row) => ({
                    permission: row.description,
                    ...Object.fromEntries(
                      roleList.map((role) => [role.key, heldMark(row.held[role.key])]),
                    ),
                  })}
                />
                <ul className="list-disc space-y-1 pr-4 pl-8 text-body text-subtle">
                  <li>Anyone can leave the organization, whatever their role.</li>
                  <li>You can give a role only if it holds nothing your own role lacks.</li>
                  <li>You can remove only members whose role holds less than yours.</li>
                  <li>The last owner can't leave or step down until someone else is an owner.</li>
                </ul>
              </div>
            )}
          </QueryBoundary>
        )}
      </QueryBoundary>
    </Panel>
  );
}

function MemberTable({ rows, isLoading }: { rows: Member[] | undefined; isLoading: boolean }) {
  const { sessionEmail } = useAuth();
  const roles = useRoles().data ?? [];
  const held = useOrganizationDetail().data?.permissions ?? [];
  const canAssign = useCan("members:assign_roles");
  const canRemove = useCan("members:remove");
  const change = useChangeMemberRole();
  const [removing, setRemoving] = useState<Member | null>(null);
  const options = grantable(roles, held).map((role) => ({ value: role.key, label: role.name }));
  const roleName = (key: string) => roles.find((role) => role.key === key)?.name ?? key;

  return (
    <Panel>
      <DataTable
        label="Members"
        head={[
          { key: "name", content: "Name" },
          { key: "email", content: "Email" },
          { key: "role", content: "Role" },
          { key: "since", content: "Joined" },
          { key: "actions", content: "", align: "end" },
        ]}
        rows={rows}
        isLoading={isLoading}
        rowKey={(member) => member.user_id}
        emptyView={<EmptyState header="No members yet" />}
        cells={(member) => {
          const self = member.email === sessionEmail;
          return {
            name: (
              <span className="inline-flex items-center gap-2 font-medium">
                {member.name}
                {self && <Lozenge>you</Lozenge>}
              </span>
            ),
            email: (
              <span className="inline-flex items-center gap-2">
                {member.email}
                {!member.verified && <Lozenge>unverified</Lozenge>}
              </span>
            ),
            role:
              canAssign && options.length > 0 ? (
                <Select
                  label={`Role of ${member.name}`}
                  labelHidden
                  spacing="compact"
                  width="medium"
                  value={member.role}
                  options={options}
                  onChange={(event) =>
                    change.mutate(
                      { userId: member.user_id, role: event.target.value },
                      { onError: refused(`Couldn't change the role of ${member.name}`) },
                    )
                  }
                />
              ) : (
                roleName(member.role)
              ),
            since: <RelativeTime iso={member.created_at} />,
            actions:
              self || canRemove ? (
                <Button appearance="subtle" spacing="compact" onClick={() => setRemoving(member)}>
                  {self ? "Leave" : "Remove"}
                </Button>
              ) : null,
          };
        }}
      />
      {removing && (
        <RemoveMember
          member={removing}
          self={removing.email === sessionEmail}
          onClose={() => setRemoving(null)}
        />
      )}
    </Panel>
  );
}

function RemoveMember({
  member,
  self,
  onClose,
}: {
  member: Member;
  self: boolean;
  onClose: () => void;
}) {
  const remove = useRemoveMember();
  return (
    <ConfirmDialog
      title={self ? "Leave this organization?" : `Remove ${member.name}?`}
      consequence={
        self
          ? "You lose access to its projects, runtimes and tickets until someone invites you again."
          : `${member.email} loses access to this organization's projects, runtimes and tickets.`
      }
      confirmLabel={self ? "Leave" : "Remove"}
      isPending={remove.isPending}
      error={remove.error}
      onCancel={onClose}
      onConfirm={() =>
        remove.mutate(member.user_id, {
          onSuccess: () => {
            if (self) {
              window.location.assign(orgless("/onboarding"));
              return;
            }
            showFlag({ appearance: "success", title: `Removed ${member.name}` });
            onClose();
          },
        })
      }
    />
  );
}

function InviteTable({ rows, isLoading }: { rows: Invite[] | undefined; isLoading: boolean }) {
  const revoke = useRevokeInvite();
  const roles = useRoles().data ?? [];
  const members = useMembers().data ?? [];
  const [renewing, setRenewing] = useState<Invite | null>(null);
  return (
    <Panel>
      <DataTable
        label="Pending invites"
        head={[
          { key: "email", content: "Email" },
          { key: "role", content: "Role" },
          { key: "by", content: "Invited by" },
          { key: "expires", content: "Expires" },
          { key: "actions", content: "", align: "end" },
        ]}
        rows={rows}
        isLoading={isLoading}
        rowKey={(invite) => invite.id}
        emptyView={
          <EmptyState
            header="No pending invites"
            description="An invite waits here until the person accepts it, or it expires."
            primaryAction={
              <LinkButton to="/members/invite" appearance="primary">
                Invite people
              </LinkButton>
            }
          />
        }
        cells={(invite) => ({
          email: invite.email,
          role: roles.find((role) => role.key === invite.role)?.name ?? invite.role,
          by: inviterName(invite.created_by, members),
          expires: <RelativeTime iso={invite.expires_at} />,
          actions: (
            <span className="inline-flex gap-1">
              <Button appearance="subtle" spacing="compact" onClick={() => setRenewing(invite)}>
                New link
              </Button>
              <Button
                appearance="subtle"
                spacing="compact"
                onClick={() =>
                  revoke.mutate(invite.id, {
                    onError: refused(`Couldn't revoke the invite for ${invite.email}`),
                  })
                }
              >
                Revoke
              </Button>
            </span>
          ),
        })}
      />
      {renewing && <RenewInvite invite={renewing} onClose={() => setRenewing(null)} />}
    </Panel>
  );
}

/** A new link for a pending invite: the API answers an invite to an address that already has one
 *  by revoking the old one, so the link sent before stops working and the expiry starts again. */
function RenewInvite({ invite, onClose }: { invite: Invite; onClose: () => void }) {
  const create = useCreateInvite();
  if (create.data) {
    return (
      <ModalDialog
        title="New invite link"
        width="medium"
        onClose={onClose}
        footer={
          <Button appearance="primary" onClick={onClose}>
            Done
          </Button>
        }
      >
        <InviteLink invite={create.data} />
      </ModalDialog>
    );
  }
  return (
    <ConfirmDialog
      title={`New link for ${invite.email}?`}
      consequence="The link sent before stops working. The new one gets a fresh expiry."
      confirmLabel="Make new link"
      appearance="warning"
      isPending={create.isPending}
      error={create.error}
      onCancel={onClose}
      onConfirm={() => create.mutate({ email: invite.email, role: invite.role })}
    />
  );
}
