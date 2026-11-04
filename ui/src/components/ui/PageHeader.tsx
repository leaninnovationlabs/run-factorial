/**
 * PageHeader and usePageTitle, after ADS.
 *
 * Every page opens with the same shape: what it is (the one `h1`), what can be done to it (at most
 * one primary action), and the facts about it (the bottom bar). Where it sits is the frame's to
 * say — the top bar names the organization and the project, the side navigation the page — so the
 * header carries no breadcrumbs. It renders in every state — loading, failed, not found — so the
 * page never jumps when the data lands.
 */

import { type ReactNode, useEffect } from "react";
import { cx } from "./cx";
import { StaleNote } from "./messages";
import { Skeleton } from "./states";
import { Tooltip } from "./Tooltip";

/** "#204 · widgets · Factorial": the tab says which page it is, most specific first. */
export function pageTitle(parts: (string | null | undefined)[]): string {
  return [...parts.filter((part): part is string => Boolean(part)), "Factorial"].join(" · ");
}

export function usePageTitle(parts: (string | null | undefined)[]): void {
  const title = pageTitle(parts);
  useEffect(() => {
    document.title = title;
  }, [title]);
}

export function PageHeader({
  title,
  fullTitle,
  titleAfter,
  actions,
  bottomBar,
  isLoading = false,
  staleSince = null,
}: {
  title: ReactNode;
  /** The title as plain text, for one that can run long — an issue's, which no one here chose.
   *  Given, the title keeps to one line in at most 60% of the row, gives way before the actions
   *  do, and shows whole on hover wherever it is cut short. */
  fullTitle?: string;
  /** Beside the title: a status lozenge, a count. */
  titleAfter?: ReactNode;
  /** At most one primary action per page. */
  actions?: ReactNode;
  /** The facts under the title: status, owner, repository, spend. */
  bottomBar?: ReactNode;
  /** The title is not known yet; its place is held at its own height. */
  isLoading?: boolean;
  /** When the page's data was last read, if its latest refetch failed. */
  staleSince?: number | null;
}) {
  const truncates = fullTitle !== undefined;
  // Focusable from script, not by Tab: the shell moves the focus here after the page changes, so
  // a keyboard or screen-reader user starts reading at the new page.
  const heading = (
    <h1
      data-page-title
      tabIndex={-1}
      className={cx("min-w-0 text-heading-lg text-default", truncates ? "truncate" : "break-words")}
    >
      {title}
    </h1>
  );
  return (
    <header className="space-y-2">
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        {/* A truncating title's column starts from nothing and grows into what the actions
            leave, so a wider actions group — a note under its buttons — shortens the title
            rather than wrapping the whole group onto a row of its own. */}
        <div
          className={cx(
            "flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1",
            truncates && "flex-1 md:max-w-[60%]",
          )}
        >
          {isLoading ? (
            <Skeleton width="16rem" height="1.75rem" />
          ) : truncates ? (
            <Tooltip content={fullTitle} position="bottom-start" onlyIfTruncated>
              {heading}
            </Tooltip>
          ) : (
            heading
          )}
          {titleAfter}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
      {bottomBar && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-body text-subtle">
          {bottomBar}
        </div>
      )}
      <StaleNote updatedAt={staleSince} />
    </header>
  );
}
