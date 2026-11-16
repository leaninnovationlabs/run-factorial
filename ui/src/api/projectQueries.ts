/** Projects and the factory board: their reads, and the writes that change a project. */

import { type QueryFilters, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { keys, LIVE_MS } from "./keys";
import type {
  GitHubConnectionIn,
  PermitsChange,
  ProjectIn,
  ProjectPatch,
  SecretDeliveryWrite,
  SecretWrite,
  SeedSource,
  Span,
  TicketSourceIn,
  TicketStatus,
} from "./types";

/** `live` is the list page's: admission changes under it. The project switcher in the top bar
 *  reads the same rows and needs no interval — a name changes about never, and it is mounted on
 *  every page of every project. */
export function useProjects({ live = true }: { live?: boolean } = {}) {
  return useQuery({
    queryKey: keys.projects,
    queryFn: api.listProjects,
    refetchInterval: live ? LIVE_MS : false,
  });
}

/** `id` may be 0 while a parent query is still resolving which project this is; that is not a
 *  request worth making, so the query stays idle until there is a real id. */
export function useProject(id: number) {
  return useQuery({
    queryKey: keys.project(id),
    queryFn: () => api.getProject(id),
    enabled: id > 0,
    // Ticket counts on this payload are what the Tickets heading and the All filter read.
    // Without a poll they freeze at whatever was true when the page opened, so a ticket the
    // worker just admitted never appears to have arrived.
    refetchInterval: LIVE_MS,
  });
}

/** The repo-side readiness rows, which the project's own read no longer waits for.
 *
 *  These are live GitHub reads, so they run only where their answer is drawn: a configuration
 *  page whose row is one of them and the Project settings overview ask on arrival, the overview's
 *  Check repository button asks again, and the side
 *  navigation, mounted on every page of the project, only reads what one of those cached. A
 *  checklist that polled would spend a round trip a second on facts a human changes about twice
 *  a year.
 *
 *  The key sits UNDER the project's, so the mutations that already invalidate `keys.project(id)`
 *  — creating the missing labels, syncing a new version — refresh these rows with it, wherever
 *  a page is observing them. */
export function useProjectReadiness(id: number, { enabled = true }: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: keys.projectReadiness(id),
    queryFn: () => api.getProjectReadiness(id),
    enabled: enabled && id > 0,
  });
}

/** Open issues the admission pass declines, recomputed per request — the answer has to read as
 *  current, and an append-only log cannot say whether a refusal is still true. Each request makes
 *  live GitHub calls, so `enabled` polls it only while the Tickets page shows it. */
export function usePendingIssues(id: number, enabled = true) {
  return useQuery({
    queryKey: keys.pendingIssues(id),
    queryFn: () => api.listPendingIssues(id),
    enabled: enabled && id > 0,
    refetchInterval: LIVE_MS,
  });
}

export function useBoard(id: number) {
  return useQuery({
    queryKey: keys.board(id),
    queryFn: () => api.getBoard(id),
    refetchInterval: LIVE_MS,
  });
}

/**
 * The project dashboard's one read. No interval, on purpose: the page is mounted only while it
 * is open, so this fetches when it opens and when the span changes, and again on the page's
 * Refresh (`useRefreshFleet` refetches it last), a reload, or a return to the window. Nine
 * aggregates every ten seconds would be spent on figures that move once a tick at most.
 */
export function useDashboard(id: number, span: Span) {
  return useQuery({
    queryKey: keys.dashboard(id, span),
    queryFn: () => api.getDashboard(id, span),
    enabled: id > 0,
    // A span switch redraws every figure from the new body; the old one stays up until it lands,
    // so the frame does not blank between the two. Another project's body is never the frame.
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[1] === id ? previous : undefined,
  });
}

/** `enabled` is for a filter whose rows do not come from this route at all — the project page's
 *  Admission filter — which must not keep the whole ticket list polling behind it. */
export function useTickets(id: number, status?: TicketStatus, openOnly?: boolean, enabled = true) {
  return useQuery({
    queryKey: keys.tickets(id, status, openOnly),
    queryFn: () => api.listTickets(id, { status, openOnly }),
    enabled,
    refetchInterval: LIVE_MS,
    // Keep the previous rows on screen while a filter change refetches, so the table does not
    // blink through an empty state on every click.
    placeholderData: (previous) => previous,
  });
}

/** The factory board. Live, like the other board views. `enabled` is for a reader that only
 *  wants it on demand — the top bar's search — and should not keep it polling on every page. */
export function useOverview({ enabled = true }: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: keys.overview,
    queryFn: () => api.getOverview(),
    enabled,
    refetchInterval: LIVE_MS,
    // The board's contract is that nothing moves. Blanking it on every refetch would break that
    // harder than any layout change could.
    placeholderData: (previous) => previous,
  });
}

// --- mutations -----------------------------------------------------------------------------
//
// Every one of these changes something the project page displays — `blockers` above all, which
// is recomputed server-side from the ticket source and the process versions. So they invalidate
// the project rather than patching a cached copy: an optimistic update here would have to
// reimplement the server's readiness rules in the client, which is the one thing `blockers`
// exists to avoid.

export function useCreateProject() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: ProjectIn) => api.createProject(body),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.projects }),
  });
}

/** The project and everything under its key, plus the list row that summarises it — by exact
 *  key, since the list's key is a prefix of every project's. */
function useProjectMutation<TArgs, TResult>(id: number, fn: (args: TArgs) => Promise<TResult>) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      client.invalidateQueries({ queryKey: keys.project(id) });
      client.invalidateQueries({ queryKey: keys.projects, exact: true });
    },
  });
}

/** The least a refresh's button stays pending, unless its refetch brings a change sooner: a
 *  spinner that flashes for a frame reads as a button that did nothing. */
export const REFRESH_MIN_MS = 1_000;
const REFRESH_WATCH_MS = 250;

/** Wakes the worker for a fleet pass, then refetches the page's reads until one changes or
 *  `REFRESH_MIN_MS` has passed since the press. A change is a new reference: a refetch that
 *  brings equal data keeps the old one (structural sharing). The dashboard is refetched once at
 *  the end, not watched: every answer carries a new `as_of`, so it would always read as changed
 *  and end the wait before the worker's pass lands. */
export function useRefreshFleet(id?: number) {
  const client = useQueryClient();
  const watched: QueryFilters[] =
    id === undefined
      ? [{ queryKey: keys.projects, exact: true }]
      : [
          { queryKey: keys.project(id), exact: true },
          { queryKey: keys.board(id) },
          { queryKey: ["projects", id, "tickets"] },
        ];
  const snapshot = () =>
    watched.flatMap((filters) => client.getQueriesData(filters).map(([, data]) => data));
  return useMutation({
    mutationFn: async () => {
      const settleAt = Date.now() + REFRESH_MIN_MS;
      const before = snapshot();
      const result = await api.refreshFleet();
      while (true) {
        await Promise.all(
          watched.map((filters) => client.refetchQueries({ ...filters, type: "active" })),
        );
        const after = snapshot();
        if (after.length !== before.length || after.some((data, i) => data !== before[i])) break;
        const left = settleAt - Date.now();
        if (left <= 0) break;
        await pause(Math.min(REFRESH_WATCH_MS, left));
      }
      if (id !== undefined) {
        await client.refetchQueries({ queryKey: ["projects", id, "dashboard"], type: "active" });
      }
      return result;
    },
  });
}

const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** Only a new repository changes what the repo-side probes read, so a name, description or
 *  settings edit refetches the project alone: invalidating the whole key re-ran the live
 *  GitHub calls behind `/readiness` on every permit ticked. */
export function useUpdateProject(id: number) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: ProjectPatch) => api.updateProject(id, body),
    onSuccess: (_, body) => {
      client.invalidateQueries({
        queryKey: keys.project(id),
        exact: body.github_repo === undefined,
      });
      client.invalidateQueries({ queryKey: keys.projects, exact: true });
    },
  });
}

/** A permit change the server applies to the settings as they stand, so two in flight both land
 *  and an Undo is the reverse change. Refetches the project alone, as a settings edit does. */
export function useChangePermits(id: number) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: PermitsChange) => api.changePermits(id, body),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: keys.project(id), exact: true });
      client.invalidateQueries({ queryKey: keys.projects, exact: true });
    },
  });
}

export function useSetProjectActive(id: number) {
  return useProjectMutation(id, (active: boolean) => api.setProjectActive(id, active));
}

export function usePutTicketSource(id: number) {
  return useProjectMutation(id, (body: TicketSourceIn) => api.putTicketSource(id, body));
}

export function useGitHubConnection(id: number) {
  return useQuery({
    queryKey: keys.githubConnection(id),
    queryFn: () => api.getGitHubConnection(id),
    enabled: id > 0,
  });
}

export function usePutGitHubConnection(id: number) {
  return useProjectMutation(id, (body: GitHubConnectionIn) => api.putGitHubConnection(id, body));
}

export function usePutSecret(id: number) {
  return useProjectMutation(id, ({ name, ...body }: { name: string } & SecretWrite) =>
    api.putSecret(id, name, body),
  );
}

export function usePatchSecretDelivery(id: number) {
  return useProjectMutation(id, ({ name, ...body }: { name: string } & SecretDeliveryWrite) =>
    api.patchSecretDelivery(id, name, body),
  );
}

export function useDeleteSecret(id: number) {
  return useProjectMutation(id, (name: string) => api.deleteSecret(id, name));
}

export function useCreateLabels(id: number) {
  return useProjectMutation(id, () => api.createLabels(id));
}

export function useCreateConfigBranch(id: number) {
  return useProjectMutation(id, ({ from }: { from: SeedSource }) =>
    api.createConfigBranch(id, from),
  );
}
