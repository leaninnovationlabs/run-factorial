/**
 * The URL's organization, checked against the ones the reader reaches. A slug none carries is
 * said, with a way to each that does, rather than shown as pages that all fail: every read under
 * it would answer 404. Pages render while the list loads; a confirmed slug is the one a bare link
 * moves into next time (`org.tsx`).
 *
 * The list holds only the reader's organizations (design.md §7.19). None: the reader goes to
 * onboarding, which waits for an invite where nobody creates organizations. A bare link that fell
 * back to `default`, which they are not in: their first organization instead of a page saying
 * `default` does not exist.
 */

import { type ReactNode, useEffect } from "react";
import { useCurrentOrganization } from "../../api/organizationQueries";
import { DEFAULT_ORG, inOrganization, orgless, rememberOrganization, useOrg } from "../../org";
import { EmptyState, PageHeader, usePageTitle } from "../ui";

export function OrganizationGate({ children }: { children: ReactNode }) {
  const org = useOrg();
  const { data, organization } = useCurrentOrganization();
  const elsewhere =
    data && !organization
      ? data.length === 0
        ? orgless("/onboarding")
        : org === DEFAULT_ORG && data[0]
          ? inOrganization(data[0].slug, "/overview")
          : null
      : null;
  useEffect(() => {
    if (organization) rememberOrganization(organization.slug);
  }, [organization]);
  useEffect(() => {
    if (elsewhere) window.location.replace(elsewhere);
  }, [elsewhere]);
  if (elsewhere) return null;
  if (data && !organization) {
    return (
      <UnknownOrganization org={org} known={data.map((o) => ({ slug: o.slug, name: o.name }))} />
    );
  }
  return children;
}

function UnknownOrganization({
  org,
  known,
}: {
  org: string;
  known: { slug: string; name: string }[];
}) {
  usePageTitle(["Organization not found"]);
  return (
    <div className="mx-auto max-w-page-narrow space-y-6 px-4 py-6">
      <PageHeader title="Organization not found" />
      <EmptyState
        header={`There is no organization named ${org}`}
        description="The link may be old, or the address may be wrong."
        primaryAction={
          <ul className="space-y-1">
            {known.map((o) => (
              <li key={o.slug}>
                {/* A full load, not a router link: the organization is the router's basename. */}
                <a href={inOrganization(o.slug, "/overview")} className="font-medium underline">
                  {o.name}
                </a>
              </li>
            ))}
          </ul>
        }
      />
    </div>
  );
}
