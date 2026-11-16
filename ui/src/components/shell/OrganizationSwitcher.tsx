/**
 * The organization the page acts in, first after the mark in the top bar, and the way to another.
 * Its name is a link to the organization's overview; the chevron beside it opens a menu of the
 * reader's organizations, the current one checked, and, where people may make them,
 * Create organization, a page of its own, and Join an organization, the page outside any
 * organization that takes a pasted invite link. Choosing another organization, or joining, is a
 * full load, never a router link: the organization is the router's basename and fixed for a page's
 * life, so no cached query can answer for the wrong tenant (`org.tsx`). Where there is nowhere else
 * to go, one organization on a deployment not known to run many, it is the link alone: the
 * deployment's answer comes after the first paint, and a chevron drawn until then would vanish on
 * every load of a deployment that runs one. Either way the name, cut short, is in full under the
 * pointer.
 *
 * The navigation drawn over the page covers the top bar, so it carries this too (`SideNav`).
 */

import { useRef } from "react";
import { useNavigate } from "react-router-dom";
import { useAuthProviders, useCurrentOrganization } from "../../api/organizationQueries";
import { plural } from "../../format";
import { inOrganization, orgless, useOrg } from "../../org";
import { DropdownMenu, NavPlace, NavPlaceLink, NavPlaceMenuButton } from "../ui";

export function OrganizationSwitcher({
  onNavigate = () => {},
}: {
  /** After a choice: the overlay navigation closes itself. */
  onNavigate?: () => void;
}) {
  const org = useOrg();
  const navigate = useNavigate();
  const anchor = useRef<HTMLDivElement | null>(null);
  const { data, organization } = useCurrentOrganization();
  const multiOrg = useAuthProviders().data?.multi_org;
  const name = organization?.name ?? org;
  const organizations = [...(data ?? [])].sort((a, b) => a.name.localeCompare(b.name));
  const home = (
    <NavPlaceLink
      data-org-name
      to="/overview"
      icon="organization"
      name={name}
      onClick={onNavigate}
    />
  );
  if (multiOrg !== true && organizations.length <= 1) return <NavPlace>{home}</NavPlace>;
  const label = `Switch organization: ${name}`;
  return (
    <DropdownMenu
      placement="bottom-start"
      anchorRef={anchor}
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
                onClick: () => {
                  onNavigate();
                  navigate("/organizations/new");
                },
              },
              {
                label: "Join an organization",
                onClick: () => window.location.assign(orgless("/join")),
              },
            ]
          : []),
      ]}
      trigger={{
        label,
        render: (props, { isOpen }) => (
          <NavPlace ref={anchor} isOpen={isOpen}>
            {home}
            <NavPlaceMenuButton {...props} data-org-switcher label={label} isOpen={isOpen} />
          </NavPlace>
        ),
      }}
    />
  );
}
