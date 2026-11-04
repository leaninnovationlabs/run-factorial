import type { ReactNode } from "react";
import { useOrganizationDetail } from "../../api/organizationQueries";
import { EmptyState, LinkButton, PageHeader, Spinner, usePageTitle } from "../ui";

/** A page whose reads need `permission`, drawn only for a reader who holds it. The API refuses the
 *  rest anyway; this says so once, in place of a page of refusals. */
export function RequirePermission({
  permission,
  children,
}: {
  permission: string;
  children: ReactNode;
}) {
  const organization = useOrganizationDetail();
  if (organization.isPending) return <Spinner label="Checking your access" />;
  if (organization.data?.permissions.includes(permission)) return children;
  return <NoAccess />;
}

function NoAccess() {
  usePageTitle(["No access"]);
  return (
    <div className="space-y-6">
      <PageHeader title="No access" />
      <EmptyState
        header="This page is for admins"
        description="Your role in this organization doesn't include it. Ask an admin if you need it."
        primaryAction={
          <LinkButton to="/overview" appearance="primary">
            Go to Overview
          </LinkButton>
        }
      />
    </div>
  );
}
