/**
 * The organization the page acts in, at the foot of the side navigation, and the way to another.
 * Choosing one is a full load of its overview, never a router link: the organization is the
 * router's basename and fixed for a page's life, so no cached query can answer for the wrong
 * tenant (`org.tsx`).
 */

import { useCurrentOrganization, useMultiOrg } from "../../api/organizationQueries";
import { plural } from "../../format";
import { inOrganization, orgless, useOrg } from "../../org";
import { DropdownMenu, NavButtonItem } from "../ui";

export function OrganizationSwitcher({ isCompact }: { isCompact: boolean }) {
  const org = useOrg();
  const { data, organization } = useCurrentOrganization();
  const multiOrg = useMultiOrg();
  const name = organization?.name ?? org;
  const organizations = [...(data ?? [])].sort((a, b) => a.name.localeCompare(b.name));
  const label = `Switch organization: ${name}`;
  return (
    <DropdownMenu
      placement="top-start"
      items={[
        {
          group: "Organizations",
          items: organizations.map((each) => ({
            label: each.name,
            description: plural(each.project_count, "project"),
            isSelected: each.slug === org,
            onClick: () => {
              if (each.slug !== org) window.location.assign(inOrganization(each.slug, "/overview"));
            },
          })),
        },
        ...(multiOrg
          ? [
              {
                label: "Create organization",
                onClick: () => window.location.assign(orgless("/onboarding")),
              },
            ]
          : []),
      ]}
      trigger={{
        label,
        render: (props, { isOpen }) => (
          <NavButtonItem
            {...props}
            data-org-switcher
            icon="organization"
            label={label}
            isCompact={isCompact}
            isOpen={isOpen}
          >
            {name}
          </NavButtonItem>
        ),
      }}
    />
  );
}
