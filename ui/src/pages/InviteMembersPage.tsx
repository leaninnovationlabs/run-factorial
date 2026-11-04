/**
 * Invite someone to the organization: an email and a role, answered with a link to send them.
 * Its own page, like `/runtimes/new`; Cancel returns to `/members`, and the answer's Back to its
 * pending invites, where the new one now waits.
 *
 * The link is in the create answer only: the mutation drops it when this page unmounts
 * (`useCreateInvite`), so it is shown once, here.
 */

import { useState } from "react";
import { useCreateInvite, useOrganizationDetail, useRoles } from "../api/organizationQueries";
import { InviteLink } from "../components/account/InviteLink";
import {
  Button,
  ErrorMessage,
  Form,
  FormFooter,
  LinkButton,
  PageHeader,
  Panel,
  RequiredLegend,
  Select,
  TextField,
  usePageTitle,
} from "../components/ui";
import { grantable, INVITES_VIEW } from "./MembersPage";

export default function InviteMembersPage() {
  usePageTitle(["Invite people", "Members"]);
  const roles = useRoles().data ?? [];
  const held = useOrganizationDetail().data?.permissions ?? [];
  const options = grantable(roles, held).map((role) => ({ value: role.key, label: role.name }));
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("member");
  const create = useCreateInvite();
  const emailProblem = email.trim() === "" ? "Enter an email." : null;

  return (
    <div className="space-y-6">
      <PageHeader title="Invite people" />
      {create.data ? (
        <Panel title="Invite link">
          <div className="space-y-4 p-4">
            <InviteLink invite={create.data} />
            <FormFooter align="end">
              <Button
                appearance="subtle"
                onClick={() => {
                  create.reset();
                  setEmail("");
                }}
              >
                Invite another
              </Button>
              <LinkButton to={`/members?view=${INVITES_VIEW}`} appearance="primary">
                Back to members
              </LinkButton>
            </FormFooter>
          </div>
        </Panel>
      ) : (
        <Panel>
          <Form
            aria-label="Invite someone"
            validate={() => !emailProblem}
            onSubmit={() => create.mutate({ email: email.trim(), role })}
            className="space-y-4 p-4"
          >
            {({ attempted }) => (
              <>
                <RequiredLegend />
                <TextField
                  label="Email"
                  type="email"
                  isRequired
                  autoFocus
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  errorMessage={attempted ? (emailProblem ?? undefined) : undefined}
                />
                <Select
                  label="Role"
                  isRequired
                  value={role}
                  options={options}
                  onChange={(event) => setRole(event.target.value)}
                />
                <ErrorMessage error={create.error} />
                <FormFooter align="end">
                  <LinkButton to="/members" appearance="subtle">
                    Cancel
                  </LinkButton>
                  <Button type="submit" appearance="primary" isLoading={create.isPending}>
                    Create invite link
                  </Button>
                </FormFooter>
              </>
            )}
          </Form>
        </Panel>
      )}
    </div>
  );
}
