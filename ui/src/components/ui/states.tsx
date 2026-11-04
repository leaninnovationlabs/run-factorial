/**
 * Every state a view can be in, designed once (ADS empty state, spinner, skeleton): loading, a
 * first load that failed, a refetch that failed while there is data on screen, nothing yet, no
 * results for a filter, and a render that threw.
 */

import type { UseQueryResult } from "@tanstack/react-query";
import { Component, type ErrorInfo, type ReactNode, useEffect, useState } from "react";
import { cx } from "./cx";
import { ErrorDetails, ErrorMessage, SectionMessage } from "./messages";
import { SpinnerGlyph } from "./SpinnerGlyph";

export function EmptyState({
  header,
  description,
  primaryAction,
  secondaryAction,
  headingLevel = 2,
  width = "wide",
}: {
  header: string;
  description?: ReactNode;
  primaryAction?: ReactNode;
  secondaryAction?: ReactNode;
  headingLevel?: 2 | 3;
  width?: "narrow" | "wide";
}) {
  const Heading = headingLevel === 2 ? "h2" : "h3";
  return (
    <div
      className={cx(
        "mx-auto flex flex-col items-center gap-2 px-4 py-10 text-center",
        width === "narrow" ? "max-w-sm" : "max-w-md",
      )}
    >
      <Heading className="text-heading-sm text-default">{header}</Heading>
      {description && <div className="text-body text-subtle">{description}</div>}
      {(primaryAction || secondaryAction) && (
        <div className="flex flex-wrap items-center justify-center gap-2 pt-2">
          {primaryAction}
          {secondaryAction}
        </div>
      )}
    </div>
  );
}

/**
 * Loading, announced. Invisible for its first `delay` ms: an answer that lands in that time never
 * flashes a spinner at the reader. The label is always there for a screen reader.
 */
export function Spinner({
  label = "Loading",
  size = "medium",
  delay = 150,
  inline = false,
}: {
  label?: string;
  size?: "small" | "medium" | "large";
  delay?: number;
  /** Beside other content, rather than centred in a region of its own. */
  inline?: boolean;
}) {
  const [shown, setShown] = useState(delay === 0);
  useEffect(() => {
    if (delay === 0) return;
    const timer = setTimeout(() => setShown(true), delay);
    return () => clearTimeout(timer);
  }, [delay]);
  return (
    <div
      role="status"
      className={cx(
        "flex items-center gap-2 text-body text-subtle",
        inline ? "inline-flex" : "justify-center px-4 py-8",
      )}
    >
      <span className={cx("inline-flex", !shown && "invisible")}>
        <SpinnerGlyph size={size} />
      </span>
      <span className={inline ? undefined : "sr-only"}>{label}</span>
    </div>
  );
}

/** A grey block the shape of what is loading, so the layout does not jump when it lands. */
export function Skeleton({
  width = "100%",
  height = "1rem",
  className,
}: {
  width?: string | number;
  height?: string | number;
  className?: string;
}) {
  return (
    <span
      aria-hidden="true"
      style={{ width, height }}
      className={cx(
        "block rounded-sm bg-skeleton animate-pulse motion-reduce:animate-none",
        className,
      )}
    />
  );
}

export function SkeletonRows({ rows = 3, columns = 4 }: { rows?: number; columns?: number }) {
  return (
    <div role="status" aria-label="Loading" className="space-y-3 px-4 py-4">
      {Array.from({ length: rows }, (_, row) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: placeholder rows have no identity but their place
        <div key={row} className="flex gap-4">
          {Array.from({ length: columns }, (_, column) => (
            <Skeleton
              // biome-ignore lint/suspicious/noArrayIndexKey: as above
              key={column}
              width={column === 0 ? "30%" : `${Math.max(10, 20 - column * 3)}%`}
            />
          ))}
        </div>
      ))}
    </div>
  );
}

/**
 * A query's loading and failure states, so a view writes only what it shows when there is data.
 *
 * Two failures, answered differently. A failure with nothing loaded yet shows the error and a way
 * to try again. A refetch that fails while data is on screen keeps the data — the view goes on
 * rendering — and `staleSince` goes to the page header, which says the data is from earlier.
 */
export function QueryBoundary<T>({
  query,
  loading,
  empty,
  isEmpty,
  errorTitle = "Couldn't load this",
  children,
}: {
  query: Pick<UseQueryResult<T>, "data" | "error" | "isPending" | "refetch">;
  loading?: ReactNode;
  empty?: ReactNode;
  isEmpty?: (data: T) => boolean;
  errorTitle?: string;
  children: (data: T) => ReactNode;
}) {
  if (query.data === undefined) {
    if (query.error) {
      return (
        <ErrorMessage error={query.error} title={errorTitle} onRetry={() => query.refetch()} />
      );
    }
    return <>{loading ?? <Spinner />}</>;
  }
  if (empty !== undefined && isEmpty?.(query.data)) return <>{empty}</>;
  return <>{children(query.data)}</>;
}

/** When a query last answered, if its latest refetch failed; the page header says so. */
export function staleSince(query: { error: unknown; data: unknown; dataUpdatedAt: number }) {
  return query.error && query.data !== undefined && query.dataUpdatedAt
    ? query.dataUpdatedAt
    : null;
}

type BoundaryState = { error: Error | null };

/**
 * A render that throws takes down its route, not the app: the shell and the navigation stay, and
 * the page says what went wrong with a way to reload. One per page (`AppShell`), reset by
 * navigating away.
 */
export class ErrorBoundary extends Component<{ children: ReactNode }, BoundaryState> {
  state: BoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): BoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("A page failed to render", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <SectionMessage
        appearance="error"
        title="Couldn't show this page"
        actions={[{ text: "Reload the page", onClick: () => window.location.reload() }]}
      >
        <ErrorDetails details={this.state.error.message} />
      </SectionMessage>
    );
  }
}
