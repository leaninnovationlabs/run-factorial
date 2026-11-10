/**
 * Tabs and SegmentedControl, after ADS.
 *
 * Tabs switch between views of one thing, with the ARIA tabs pattern: one tab in the tab order,
 * the arrow keys (and Home and End) move between them and select as they go, and each panel is
 * named by its tab. A SegmentedControl is a set of pressed/unpressed buttons for a filter or a
 * sort — a choice among peers that narrows what one view shows, not a different view.
 *
 * The two look apart on purpose. A tab's selection is a bar under it, drawn by its `::after` over
 * its own transparent bottom edge so the tab's box never changes (`PreviewPane` measures a copy of
 * it); the bar grows out from the centre, grey on hover and dark once chosen. A segmented
 * control is a sunken track whose chosen option is raised out of it, with the button's edge
 * and shadow (`--button-*`), so the choice keeps an edge in dark mode, where there is no shadow.
 *
 * Both are controlled: the page keeps the value, and binds it to the URL where a view should be
 * shareable by link.
 */

import { createContext, Fragment, type KeyboardEvent, type ReactNode, useContext } from "react";
import { cx } from "./cx";
import { Badge } from "./Lozenge";

type TabsState = { id: string; selected: string; onChange: (value: string) => void };

const TabsContext = /*#__PURE__*/ createContext<TabsState | null>(null);

function useTabs(): TabsState {
  const state = useContext(TabsContext);
  if (!state) throw new Error("Tab parts must be inside <Tabs>");
  return state;
}

/** A tab's and a panel's DOM ids. A value may be any text, a file's path among them, and an id
 *  reference is a list split on whitespace: encoded, a space cannot break one in two. */
export function tabId(id: string, value: string): string {
  return `${id}-tab-${encodeURIComponent(value)}`;
}

export function panelId(id: string, value: string): string {
  return `${id}-panel-${encodeURIComponent(value)}`;
}

export function Tabs({ id, selected, onChange, children }: TabsState & { children: ReactNode }) {
  return <TabsContext.Provider value={{ id, selected, onChange }}>{children}</TabsContext.Provider>;
}

export function TabList({
  label,
  align = "start",
  children,
}: {
  label: string;
  /** `fill`: the tabs share the row's width equally. */
  align?: "start" | "fill";
  children: ReactNode;
}) {
  const { onChange } = useTabs();
  function move(event: KeyboardEvent<HTMLDivElement>) {
    const tabs = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]')];
    const current = tabs.indexOf(document.activeElement as HTMLButtonElement);
    if (current === -1) return;
    const next =
      event.key === "ArrowRight"
        ? (current + 1) % tabs.length
        : event.key === "ArrowLeft"
          ? (current - 1 + tabs.length) % tabs.length
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? tabs.length - 1
              : -1;
    if (next === -1) return;
    event.preventDefault();
    const tab = tabs[next];
    tab?.focus();
    if (tab?.dataset.value) onChange(tab.dataset.value);
  }
  return (
    <div
      role="tablist"
      aria-label={label}
      tabIndex={-1}
      onKeyDown={move}
      className={cx(
        "flex gap-1 border-b border-default",
        align === "fill" && "[&>[role=tab]]:flex-1 [&>[role=tab]]:justify-center",
      )}
    >
      {children}
    </div>
  );
}

export function Tab({
  value,
  children,
  badge,
  end = false,
}: {
  value: string;
  children: ReactNode;
  badge?: number;
  /** Set apart at the far end of the row, for a view that is about the thing rather than in it. */
  end?: boolean;
}) {
  const { id, selected, onChange } = useTabs();
  const isSelected = selected === value;
  return (
    <button
      type="button"
      role="tab"
      id={tabId(id, value)}
      data-value={value}
      aria-selected={isSelected}
      // Only the shown panel exists, and a reference to one that does not is a broken one.
      aria-controls={isSelected ? panelId(id, value) : undefined}
      tabIndex={isSelected ? 0 : -1}
      onClick={() => onChange(value)}
      className={cx(
        "focus-inset relative -mb-px inline-flex h-11 items-center gap-2 border-b-2 border-transparent px-3 text-body font-medium transition-colors",
        "after:absolute after:inset-x-0 after:-bottom-0.5 after:border-b-2 after:transition after:duration-200 after:ease-out-bold motion-reduce:after:transition-none",
        end && "ml-auto",
        isSelected
          ? "text-selected after:scale-x-100 after:border-selected"
          : "text-subtle after:scale-x-0 after:border-bold hover:text-default hover:after:scale-x-100",
      )}
    >
      {children}
      {badge !== undefined && badge > 0 && <Badge>{badge}</Badge>}
    </button>
  );
}

export function TabPanel({
  value,
  flush = false,
  className,
  children,
}: {
  value: string;
  /** No gap under the tabs, for a panel drawn as a card hanging from them. */
  flush?: boolean;
  /** Layout only: a panel that fills the height left under the tabs. */
  className?: string;
  children: ReactNode;
}) {
  const { id, selected } = useTabs();
  if (selected !== value) return null;
  return (
    <div
      role="tabpanel"
      id={panelId(id, value)}
      aria-labelledby={tabId(id, value)}
      // biome-ignore lint/a11y/noNoninteractiveTabindex: the tabs pattern makes the panel a stop
      tabIndex={0}
      className={cx("focus-inset", !flush && "pt-4", className)}
    >
      {children}
    </div>
  );
}

const SEGMENT =
  "inline-flex shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-sm border font-medium transition motion-reduce:transition-none select-none";

/** The track's 2px around each segment keeps the whole control at a button's height. */
const SEGMENT_SPACING = {
  default: "h-7 px-3 text-body",
  compact: "h-5 px-2 text-body",
};

/** One segment's classes, exported for the static reference pages, which redraw the theme
 *  switch without React. */
export function segmentLook(spacing: "default" | "compact", isSelected: boolean): string {
  return cx(
    SEGMENT,
    SEGMENT_SPACING[spacing],
    isSelected
      ? "border-button bg-surface-raised text-default shadow-button"
      : "border-transparent text-subtle hover:bg-neutral-hovered hover:text-default active:bg-neutral-pressed",
  );
}

export type SegmentOption<T extends string> = {
  value: T;
  label: string;
  count?: number;
  /** A rule drawn before this option, for one that selects a different kind of thing from the
   *  ones before it rather than another slice of the same set. */
  separatorBefore?: boolean;
};

export function SegmentedControl<T extends string>({
  label,
  value,
  onChange,
  options,
  spacing = "default",
}: {
  label: string;
  value: T;
  onChange: (value: T) => void;
  options: SegmentOption<T>[];
  spacing?: "default" | "compact";
}) {
  return (
    // biome-ignore lint/a11y/useSemanticElements: a row of toggles, not a form's fieldset
    <div
      role="group"
      aria-label={label}
      className="inline-flex max-w-full flex-wrap items-center gap-0.5 rounded-md bg-neutral p-0.5"
    >
      {options.map((option) => {
        const isSelected = value === option.value;
        return (
          <Fragment key={option.value}>
            {option.separatorBefore && (
              <span aria-hidden="true" className="mx-1 h-4 shrink-0 border-l border-bold" />
            )}
            <button
              type="button"
              aria-pressed={isSelected}
              onClick={() => onChange(option.value)}
              className={segmentLook(spacing, isSelected)}
            >
              {option.label}
              {option.count !== undefined && <Badge>{option.count}</Badge>}
            </button>
          </Fragment>
        );
      })}
    </div>
  );
}
