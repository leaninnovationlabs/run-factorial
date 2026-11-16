/**
 * A section card: a surface with an edge and a titled header. No shadow — ADS keeps elevation for
 * things that move or float, and a panel is part of the page.
 */

import { type ReactNode, useId } from "react";
import { cx } from "./cx";
import { Icon } from "./icons";
import { Badge } from "./Lozenge";
import { InlineMessage } from "./messages";

/** Names a script finds the panel by, such as Show me's `data-assist-anchor`. */
type DataAttributes = { [name: `data-${string}`]: string | undefined };

export function Panel({
  title,
  subtitle,
  count,
  titleAction,
  actions,
  headingLevel = 2,
  isCollapsible = false,
  isOpen = true,
  onToggle,
  bare = false,
  note,
  className,
  children,
  ...data
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  /** A number the title is about, drawn as a badge beside it. */
  count?: number;
  /** One icon control beside the title, outside the heading so its name stays the panel's: the
   *  activity's sort. */
  titleAction?: ReactNode;
  actions?: ReactNode;
  headingLevel?: 2 | 3;
  /** The header folds the body away; `isOpen` and `onToggle` say whether it is folded. */
  isCollapsible?: boolean;
  isOpen?: boolean;
  onToggle?: () => void;
  /** No surface of its own, for a panel inside another one. */
  bare?: boolean;
  /** Behind an info icon before the subtitle: the rule the figures follow, in a sentence or two. */
  note?: ReactNode;
  /** Layout only. */
  className?: string;
  children?: ReactNode;
} & DataAttributes) {
  const headingId = useId();
  const Heading = headingLevel === 2 ? "h2" : "h3";
  const heading = (
    <span className="flex min-w-0 items-center gap-2">
      <span className="truncate">{title}</span>
      {count !== undefined && <Badge>{count}</Badge>}
    </span>
  );
  const titled = (
    <Heading id={headingId} className="text-heading-xs text-default">
      {isCollapsible ? (
        <button
          type="button"
          aria-expanded={isOpen}
          onClick={onToggle}
          className="-ml-1 inline-flex items-center gap-1 rounded-sm px-1 hover:bg-neutral-subtle-hovered"
        >
          <Icon glyph={isOpen ? "chevron-down" : "chevron-right"} className="text-icon-subtle" />
          {heading}
        </button>
      ) : (
        heading
      )}
    </Heading>
  );
  return (
    <section
      {...data}
      aria-labelledby={title ? headingId : undefined}
      className={cx(!bare && "rounded-lg border border-default bg-surface", className)}
    >
      {(title || actions) && (
        <header
          className={cx(
            "flex flex-wrap items-center justify-between gap-x-4 gap-y-2 px-4 py-3",
            isOpen && children !== undefined && "border-b border-default",
          )}
        >
          <div className="min-w-0 space-y-0.5">
            {titleAction ? (
              <div className="flex min-w-0 items-center gap-1">
                {titled}
                {titleAction}
              </div>
            ) : (
              titled
            )}
            {(note || subtitle) && (
              <div className="flex items-start gap-1.5 text-body-sm text-subtle">
                {note && (
                  <span className="shrink-0 pt-0.5" data-panel-note>
                    <InlineMessage
                      iconLabel={typeof title === "string" ? `About ${title}` : "About this panel"}
                      title={typeof title === "string" ? title : undefined}
                    >
                      {note}
                    </InlineMessage>
                  </span>
                )}
                {subtitle && <span className="min-w-0">{subtitle}</span>}
              </div>
            )}
          </div>
          {actions}
        </header>
      )}
      {/* Folded, not unmounted: what someone typed into a body survives closing it. */}
      {isCollapsible ? <div hidden={!isOpen}>{children}</div> : children}
    </section>
  );
}
