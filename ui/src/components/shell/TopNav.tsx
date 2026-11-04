/**
 * The top bar: the menu button and the product on the left, and the search in the middle. Where
 * the reader is — the project, the organization — is the side navigation's to say, and so is the
 * assistant, a page of its own; anything new is created from the list it belongs to.
 *
 * A phone has no side navigation on the page, so there the bar says where the reader is: the
 * organization beside the mark, and the project under it inside one — a ticket's page included.
 * The search is an icon at the right (`JumpSearch`'s `collapsible`), and opened it takes the room
 * the names held.
 */

import { useState } from "react";
import { Link } from "react-router-dom";
import logoUrl from "../../../logo.svg";
import { useOrganizationDetail } from "../../api/organizationQueries";
import { useProject } from "../../api/projectQueries";
import { useProjectPlace } from "../project";
import { cx, IconButton } from "../ui";
import { useIsPhone } from "../ui/media";
import { JumpSearch } from "./JumpSearch";
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
  const [searching, setSearching] = useState(false);
  const titled = phone && !searching;
  return (
    // Three columns from `md`, the outer two equal, so the search sits on the page's centre line
    // rather than in the middle of whatever the left group leaves. Narrower it is a row, the search
    // taking what the mark leaves: there the grid's flexible outer columns gave the mark less than
    // its own width, and the search was drawn over it.
    <header className="flex h-12 shrink-0 items-center gap-2 border-b border-default bg-surface px-2 sm:px-3 md:grid md:grid-cols-[1fr_minmax(0,28rem)_1fr]">
      <div className="flex shrink-0 items-center gap-2">
        {onToggleNav && (
          <IconButton
            icon="menu"
            label={navLabel}
            aria-expanded={navExpanded}
            aria-controls={SIDE_NAV_ID}
            onClick={onToggleNav}
          />
        )}
        <Link to="/overview" className="flex shrink-0 items-center gap-2 rounded-md px-1 py-1">
          {/* Named once: by the wordmark where it shows, by the alt text where it does not. */}
          <img data-nav-mark src={logoUrl} alt="" className="size-6" />
          <span className="hidden text-heading-xs text-default md:inline">Factorial</span>
          <span className="sr-only md:hidden">Factorial</span>
        </Link>
      </div>
      {titled && <PlaceTitle />}
      <div className={titled ? "shrink-0" : "min-w-0 flex-1"}>
        <JumpSearch collapsible={phone} unfolded={searching} onUnfoldedChange={setSearching} />
      </div>
      {/* The third column holds nothing; it is what keeps the search on the centre line. */}
      <div aria-hidden="true" className="hidden md:block" />
    </header>
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
