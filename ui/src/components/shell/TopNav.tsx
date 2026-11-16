/**
 * The top bar: the menu button and the mark on the left, then where the reader is — the
 * organization and, inside a project, the project, each a link to its home with a chevron beside
 * it that opens the menu that switches it — and the search in the middle. A ticket's page is inside its project. The side navigation lists
 * the pages of the place the bar names; the assistant is one of them, and anything new is created
 * from the list it belongs to.
 *
 * Below 1024px the search is an icon at the right (`JumpSearch`'s `collapsible`), so the names keep
 * the bar, and opened it takes the room they held. On a phone the bar names the place as labels,
 * the organization over the project: a name in a bar this high is too small a thing to press, so
 * the menus are in the navigation the tab bar's Menu opens.
 */

import { useState } from "react";
import { Link } from "react-router-dom";
import logoUrl from "../../../logo.svg";
import { useOrganizationDetail } from "../../api/organizationQueries";
import { useProject } from "../../api/projectQueries";
import { ProjectSwitcher, useProjectPlace } from "../project";
import { cx, IconButton } from "../ui";
import { BELOW_LG, useIsPhone, useMedia } from "../ui/media";
import { JumpSearch } from "./JumpSearch";
import { OrganizationSwitcher } from "./OrganizationSwitcher";
import { SIDE_NAV_ID } from "./SideNav";

export function TopNav({
  navLabel,
  navExpanded,
  onToggleNav,
}: {
  /** What pressing the menu button does, in words: "Collapse navigation", "Open navigation". */
  navLabel: string;
  navExpanded: boolean;
  /** Absent while a page holds the whole window (`useFocusMode`): there is no navigation to
   *  toggle. Absent on a phone too, where the tab bar's Menu opens it. */
  onToggleNav?: () => void;
}) {
  const phone = useIsPhone();
  const folds = useMedia(BELOW_LG);
  const [searching, setSearching] = useState(false);
  const named = !(folds && searching);
  return (
    // Three columns from `lg`, the outer two equal, so the search sits on the page's centre line
    // rather than in the middle of whatever the left group leaves — unless the names need more
    // than half of what the search leaves: the left column is never narrower than they are, so
    // the search moves right before a name is cut shorter than its own cap. Narrower it is a row,
    // the names taking what the mark leaves and the search folded to an icon at its end.
    <header className="flex h-12 shrink-0 items-center gap-2 border-b border-default bg-surface px-2 sm:px-3 lg:grid lg:grid-cols-[minmax(max-content,1fr)_minmax(0,28rem)_minmax(0,1fr)]">
      <div
        className={cx("flex items-center gap-2", named && !phone ? "min-w-0 flex-1" : "shrink-0")}
      >
        {onToggleNav && (
          <IconButton
            icon="menu"
            label={navLabel}
            aria-expanded={navExpanded}
            aria-controls={SIDE_NAV_ID}
            onClick={onToggleNav}
          />
        )}
        <Link to="/overview" className="flex shrink-0 items-center rounded-md p-1">
          <img data-nav-mark src={logoUrl} alt="" className="size-6" />
          <span className="sr-only">Factorial</span>
        </Link>
        {named && !phone && <PlaceSwitchers />}
      </div>
      {named && phone && <PlaceTitle />}
      <div className={folds ? (searching ? "min-w-0 flex-1" : "shrink-0") : "min-w-0"}>
        <JumpSearch collapsible={folds} unfolded={searching} onUnfoldedChange={setSearching} />
      </div>
      {/* The third column holds nothing; it is what keeps the search on the centre line. */}
      <div aria-hidden="true" className="hidden lg:block" />
    </header>
  );
}

/** The organization, and the project inside one, each cut short rather than wrapped; a tooltip
 *  says the name in full. */
function PlaceSwitchers() {
  const place = useProjectPlace();
  return (
    <div data-place-switchers className="flex min-w-0 items-center gap-1">
      <div className="min-w-0 max-w-48">
        <OrganizationSwitcher />
      </div>
      {place && (
        <>
          <span aria-hidden="true" className="shrink-0 text-subtlest">
            /
          </span>
          <div className="min-w-0 max-w-48">
            <ProjectSwitcher place={place} />
          </div>
        </>
      )}
    </div>
  );
}

/** The organization, and the project under it inside one; each cut short rather than wrapped. */
function PlaceTitle() {
  const organization = useOrganizationDetail().data?.name;
  const place = useProjectPlace();
  const project = useProject(place?.projectId ?? 0).data?.name;
  return (
    <div data-place-title className="flex min-w-0 flex-1 flex-col justify-center">
      <span
        className={cx(
          "truncate",
          project ? "text-body-sm text-subtle" : "text-heading-xs text-default",
        )}
      >
        {organization}
      </span>
      {project && <span className="truncate text-heading-xs text-default">{project}</span>}
    </div>
  );
}
