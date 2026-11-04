/** One ticket: its reads, and the requests that ask the loop to act on it. */

import { type QueryClient, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";
import { api } from "./client";
import { keys, LIVE_MS, retryUnlessClientError } from "./keys";
import type {
  ChangeRequestResult,
  CompletionResult,
  RequestResult,
  RunPreview,
  TerminalInfo,
} from "./types";

export function useTicket(ticketId: number) {
  return useQuery({
    queryKey: keys.ticket(ticketId),
    queryFn: () => api.getTicket(ticketId),
    refetchInterval: LIVE_MS,
    enabled: ticketId > 0,
  });
}

export function useTimeline(ticketId: number) {
  return useQuery({
    queryKey: keys.timeline(ticketId),
    queryFn: () => api.getTimeline(ticketId),
    refetchInterval: LIVE_MS,
  });
}

/**
 * The ticket's pinned config snapshot. Not polled: a snapshot never changes, and the ticket moves
 * to another only by a restart, which changes `processVersionId` and so the key (design.md §2.8).
 * Waits for the ticket, which is what names the version.
 */
export function useTicketProcess(ticketId: number, processVersionId: number | undefined) {
  return useQuery({
    queryKey: keys.ticketProcess(ticketId, processVersionId),
    queryFn: () => api.getTicketProcess(ticketId),
    enabled: ticketId > 0 && processVersionId !== undefined,
    staleTime: Number.POSITIVE_INFINITY,
  });
}

/**
 * The ticket's issue, for the description modal.
 *
 * `enabled` is the modal being open, or an exited ticket whose re-enter button has to know
 * whether the issue is still closed (`TicketActions`): reading the issue makes the API call
 * GitHub, so no other page view pays for it. Not polled
 * either — an issue description is edited by a human on GitHub, not by the loop, and a body that
 * rewrote itself under a reader mid-sentence would be worse than one that is a minute old.
 * Re-opening the modal refetches in the background, and the disabled button refreshes its answer
 * on remount and on window focus, so a stale read corrects itself on the next look.
 */
export function useTicketIssue(ticketId: number, enabled: boolean) {
  return useQuery({
    queryKey: keys.ticketIssue(ticketId),
    queryFn: () => api.getTicketIssue(ticketId),
    enabled: enabled && ticketId > 0,
    retry: retryUnlessClientError,
  });
}

const GATE_PULL_MS = 30_000;

/** The merge/pull request the gate is watching now — same pick the worker re-resolves each tick. */
export function useTicketGatePullRequest(ticketId: number, enabled: boolean) {
  return useQuery({
    queryKey: keys.gatePullRequest(ticketId),
    queryFn: () => api.getTicketGatePullRequest(ticketId),
    enabled: enabled && ticketId > 0,
    // The worker's tick: the pick cannot change faster, and each poll lists the repo's pull
    // requests on the deployment's one GitHub App rate limit.
    refetchInterval: enabled ? GATE_PULL_MS : false,
    retry: retryUnlessClientError,
  });
}

/**
 * A run's activity. `live` keeps it polling while the run is still going; a finished run's log
 * never changes again, so it is fetched once.
 */
export function useRunActivity(stepRunId: number, enabled: boolean, live: boolean) {
  return useQuery({
    queryKey: keys.runActivity(stepRunId),
    queryFn: () => api.getRunActivity(stepRunId),
    enabled,
    refetchInterval: live ? LIVE_MS : false,
    retry: retryUnlessClientError,
  });
}

/**
 * A run's preview URL, polled while the viewer is open — including after the run ends, because
 * a producer may publish or replace the URL later. It reads Factorial's metadata, never the site
 * behind the URL. A failed poll keeps the last answer, so a blip never blanks the frame.
 */
export function useRunPreview(stepRunId: number) {
  return useQuery({
    queryKey: keys.runPreview(stepRunId),
    queryFn: () => api.getRunPreview(stepRunId),
    enabled: stepRunId > 0,
    refetchInterval: LIVE_MS,
    retry: retryUnlessClientError,
  });
}

/** How often a recorded run's entries are asked for again: only their signed links change, and a
 *  link lasts at least an hour (`api/sessions.py::sign_page_link`). */
const LINKS_MS = 10 * 60_000;

/** A run's declared files. Polled while `live` (the run is going), while an entry waits
 *  for the worker's record, or while the last ask failed; after that only often enough to keep
 *  its links fresh. */
export function useRunOutputs(stepRunId: number, live: boolean) {
  return useQuery({
    queryKey: keys.runOutputs(stepRunId),
    queryFn: () => api.getRunOutputs(stepRunId),
    enabled: stepRunId > 0,
    refetchInterval: (query) =>
      live ||
      query.state.status === "error" ||
      query.state.data?.entries.some((entry) => entry.status === "pending")
        ? LIVE_MS
        : LINKS_MS,
    retry: retryUnlessClientError,
  });
}

/** Start, restart, keep or stop a run's preview. The answer is the preview as the woken pass left
 *  it, so it goes straight into the viewer's cache, and the viewer's polling carries on from there. */
export function usePreviewAction(stepRunId: number) {
  const qc = useQueryClient();
  const settle = (preview: RunPreview) => qc.setQueryData(keys.runPreview(stepRunId), preview);
  return {
    start: useMutation({ mutationFn: () => api.startPreview(stepRunId), onSuccess: settle }),
    restart: useMutation({
      mutationFn: () => api.startPreview(stepRunId, true),
      onSuccess: settle,
    }),
    keep: useMutation({ mutationFn: () => api.keepPreview(stepRunId), onSuccess: settle }),
    stop: useMutation({ mutationFn: () => api.stopPreview(stepRunId), onSuccess: settle }),
  };
}

/** While an open session starts or a Resume waits on the loop, the answer is seconds away. */
export const TERMINAL_STARTING_MS = 2_000;
/** While the session waits on a person: when it parks moves with typing. */
export const TERMINAL_WAITING_MS = 5_000;
/** While nothing is attachable yet and the route says that can change on its own. */
export const TERMINAL_UNSETTLED_MS = 5_000;
/** How long the same unsettled answer is polled for: one that stands this long is not about to
 *  change, and what would change it still reads the route again (`terminalPoll`). */
export const UNSETTLED_FOR_MS = 600_000;

/** How often a run's terminal is read again, from what the last read said and since when the
 *  same unsettled answer has stood. Besides these, the run's own status change (the ticket's
 *  poll), a gateway close or retry, and a later attempt of the step each read it again. */
export function terminalPoll(
  terminal: TerminalInfo | undefined,
  unsettledSince: number | null,
  now: number,
): number | false {
  if (!terminal) return false;
  if (terminal.inspect.status === "starting" || terminal.resume_pending) {
    return TERMINAL_STARTING_MS;
  }
  if (terminal.idle !== null) return TERMINAL_WAITING_MS;
  if (terminal.attachable || terminal.settled) return false;
  return now - (unsettledSince ?? now) < UNSETTLED_FOR_MS ? TERMINAL_UNSETTLED_MS : false;
}

export type Unsettled = { answer: string; since: number };

/** When the current unsettled answer was first seen: a different answer starts the cap over. */
export function trackUnsettled(
  previous: Unsettled | null,
  terminal: TerminalInfo | undefined,
  now: number,
): Unsettled | null {
  if (!terminal || terminal.attachable || terminal.settled) return null;
  const answer = JSON.stringify([terminal.kind, terminal.why_not]);
  return previous?.answer === answer ? previous : { answer, since: now };
}

/** How long after a gateway close the route is read again while it still says attachable: the
 *  loop records an open session's or a session's end on its next pass, after the gateway has closed. */
const SETTLE_MS = 60_000;

/** What a run's terminal offers, polled by `terminalPoll`, and every 2 seconds for a minute after
 *  `settling` (when the gateway closed) while the route still lags behind that close. */
export function useRunTerminal(stepRunId: number, settling: number | null = null) {
  const unsettled = useRef<Unsettled | null>(null);
  return useQuery({
    queryKey: keys.runTerminal(stepRunId),
    queryFn: () => api.getRunTerminal(stepRunId),
    enabled: stepRunId > 0,
    refetchInterval: (query) => {
      const terminal = query.state.data;
      const now = Date.now();
      unsettled.current = trackUnsettled(unsettled.current, terminal, now);
      return (
        terminalPoll(terminal, unsettled.current?.since ?? null, now) ||
        (settling !== null && terminal?.attachable && now - settling < SETTLE_MS
          ? TERMINAL_STARTING_MS
          : false)
      );
    },
    retry: retryUnlessClientError,
  });
}

/** Resume a parked session, open a session or close one. All are requests a pass honours later,
 *  so what they change is read back through `useRunTerminal`. A Resume's answer comes once the
 *  pass it woke has landed, so the ticket page is read again with it, as an interrupt's is. */
export function useTerminalRequests(stepRunId: number, ticketId: number) {
  const qc = useQueryClient();
  return {
    resume: useMutation({
      mutationFn: (answer: string | null) => api.resumeSession(stepRunId, answer),
      onSuccess: () =>
        Promise.all([
          qc.invalidateQueries({ queryKey: keys.runTerminal(stepRunId) }),
          refetchTicketPage(qc, ticketId),
        ]),
    }),
    openSession: useMutation({
      mutationFn: () => api.openSession(stepRunId),
      onSuccess: (terminal: TerminalInfo) => qc.setQueryData(keys.runTerminal(stepRunId), terminal),
    }),
    closeSession: useMutation({
      mutationFn: () => api.closeSession(stepRunId),
      onSuccess: () => qc.invalidateQueries({ queryKey: keys.runTerminal(stepRunId) }),
    }),
  };
}

/**
 * One pass over this ticket now (decision 4). The API waits for the pass, so its answer carries
 * the ticket as that pass left it and goes straight into the cache; the timeline is refetched to
 * match. Both by exact key: invalidating the `keys.ticket` prefix would also refetch the pinned
 * snapshot and, on an exited ticket, the live GitHub issue read. Returning the refetch keeps the
 * button pending until the log has caught up too.
 */
export function useRefreshTicket(ticketId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.refreshTicket(ticketId),
    onSuccess: (result) => {
      qc.setQueryData(keys.ticket(ticketId), result.ticket);
      return qc.invalidateQueries({ queryKey: keys.timeline(ticketId), exact: true });
    },
  });
}

/**
 * What a request's answer changes on the ticket page: the ticket and its log. By exact key, as
 * `useRefreshTicket` does — the `keys.ticket` prefix would also refetch the pinned snapshot and,
 * on an exited ticket, the live GitHub issue read, and the button would stay pending on both.
 * Returned from `onSuccess`, it keeps the button pending until the page has caught up.
 */
function refetchTicketPage(qc: QueryClient, ticketId: number) {
  return Promise.all([
    qc.invalidateQueries({ queryKey: keys.ticket(ticketId), exact: true }),
    qc.invalidateQueries({ queryKey: keys.timeline(ticketId), exact: true }),
  ]);
}

/**
 * A request the loop honours on the pass it wakes (decisions 29, 31): the API answers once that
 * pass has landed, so the refetch shows what it did — `interrupted`, `exited`, `running` again.
 * `alsoChanged` marks what else the answer changes, beside the ticket page.
 */
function useTicketRequest<TVariables = void, TData extends RequestResult = RequestResult>(
  ticketId: number,
  request: (variables: TVariables) => Promise<TData>,
  alsoChanged: (qc: QueryClient) => void = () => {},
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: request,
    onSuccess: () => {
      alsoChanged(qc);
      return refetchTicketPage(qc, ticketId);
    },
  });
}

export const useRequestChanges = (ticketId: number) =>
  useTicketRequest(
    ticketId,
    ({
      stepRunId,
      feedback,
      idempotencyKey,
    }: {
      stepRunId: number;
      feedback: string;
      idempotencyKey: string;
    }): Promise<ChangeRequestResult> => api.requestChanges(stepRunId, feedback, idempotencyKey),
  );

export const useInterruptRun = (ticketId: number) =>
  useTicketRequest(ticketId, (stepRunId: number) => api.interruptRun(stepRunId));

export const useRetryStep = (ticketId: number) =>
  useTicketRequest(ticketId, (stepRunId: number) => api.retryStep(stepRunId));

export const useRequestCompletion = (ticketId: number) =>
  useTicketRequest(
    ticketId,
    ({
      stepRunId,
      allowUnmet,
      seenFailures,
    }: {
      stepRunId: number;
      allowUnmet?: boolean;
      seenFailures?: string[];
    }): Promise<CompletionResult> =>
      api.requestCompletion(stepRunId, { allow_unmet: allowUnmet, seen_failures: seenFailures }),
  );

/** A Force complete's answer, polled every 2 seconds until the loop has given one. */
export function useCompletionAnswer(stepRunId: number, requestId: number | null, enabled: boolean) {
  return useQuery({
    queryKey: keys.completion(stepRunId, requestId ?? 0),
    queryFn: () => api.getCompletion(stepRunId, requestId ?? 0),
    enabled: enabled && requestId !== null,
    refetchInterval: (query) => (query.state.data?.outcome === "pending" ? 2000 : false),
  });
}

export const useExitTicket = (ticketId: number) =>
  useTicketRequest(ticketId, () => api.exitTicket(ticketId));

/** Exit, and close the issue too: offered while the gate's pull request is closed (decision 79). */
export const useCloseAndExitTicket = (ticketId: number) =>
  useTicketRequest(ticketId, () => api.exitTicket(ticketId, { close_issue: true }));

export const useReenterTicket = (ticketId: number) =>
  useTicketRequest(ticketId, () => api.reenterTicket(ticketId));

/** An escalation released the way removing `factorial:blocked` releases it. */
export const useUnblockTicket = (ticketId: number) =>
  useTicketRequest(ticketId, () => api.unblockTicket(ticketId));

/** A quarantined ticket back in the queue. `api.requeueTicket` writes directly rather than
 *  leaving a request for the loop, then waits for the pass it wakes like the rest. */
export const useRequeueTicket = (ticketId: number) =>
  useTicketRequest(ticketId, () => api.requeueTicket(ticketId));

/** What a restart would cancel, close and delete. Fetched only while its review is open: it reads
 *  the ticket's branches and pull requests off GitHub. */
export function useRestartPreview(ticketId: number, enabled: boolean) {
  return useQuery({
    queryKey: keys.restartPreview(ticketId),
    queryFn: () => api.getRestartPreview(ticketId),
    enabled: enabled && ticketId > 0,
    staleTime: 0,
  });
}

/**
 * Restart on the lane's current snapshot (#321), named by the one the review showed. Never
 * `allow_current_pin`: restarting a ticket on the snapshot it already has is a bearer or MCP
 * caller's deliberate choice. `allowMerged` is the person's answer to the review's merged work.
 * The project's lists show the ticket's step and status, so they are marked stale — not re-read
 * here, where only the project's detail is open. The pinned process needs nothing: its key
 * follows `process_version_id` (`useTicketProcess`).
 */
export const useRestartTicket = (ticketId: number, projectId: number) =>
  useTicketRequest(
    ticketId,
    ({ processVersionId, allowMerged }: { processVersionId: number; allowMerged: boolean }) =>
      api.restartTicket(ticketId, {
        process_version_id: processVersionId,
        ...(allowMerged ? { allow_merged: true } : {}),
      }),
    (qc) => void qc.invalidateQueries({ queryKey: keys.project(projectId), refetchType: "none" }),
  );

/** Redo a step from a clean start (decision 76). The project's lists show the ticket's step, so
 *  they are marked stale, as a restart marks them. */
export const useRedoStep = (ticketId: number, projectId: number) =>
  useTicketRequest(
    ticketId,
    (body: { target_step: string; note?: string }) => api.redoStep(ticketId, body),
    (qc) => void qc.invalidateQueries({ queryKey: keys.project(projectId), refetchType: "none" }),
  );
