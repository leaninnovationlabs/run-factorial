import { EmptyState, LinkButton, PageHeader, usePageTitle } from "../ui";

/** An address the app does not have. Said, rather than redirected away from in silence: a
 *  mistyped or stale link should read as one, with a way back. */
export function NotFound() {
  usePageTitle(["Page not found"]);
  return (
    <div className="space-y-6">
      <PageHeader title="Page not found" />
      <EmptyState
        header="Factorial can't find that page"
        description="The link may be old, or the address may be wrong."
        primaryAction={
          <LinkButton to="/overview" appearance="primary">
            Go to Overview
          </LinkButton>
        }
      />
    </div>
  );
}
