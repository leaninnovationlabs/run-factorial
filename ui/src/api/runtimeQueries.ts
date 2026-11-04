/** Runtimes: the list, a runtime's bindings, and the writes and login that change one. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { keys, retryUnlessClientError } from "./keys";
import type { RuntimePatch } from "./types";

// --- runtimes --------------------------------------------------------------------------------
//
// Configuration data, like the project: only this UI changes it, so nothing polls. The one
// exception is the device login, whose completion happens in the API process — the poll below
// is how the page learns of it, and the runtime list is invalidated once it has.

export function useRuntimes({ enabled = true }: { enabled?: boolean } = {}) {
  return useQuery({ queryKey: keys.runtimes, queryFn: api.listRuntimes, enabled });
}

/** What this deployment offers; fixed until it redeploys. */
export function useRuntimeOptions() {
  return useQuery({
    queryKey: keys.runtimeOptions,
    queryFn: api.runtimeOptions,
    staleTime: Number.POSITIVE_INFINITY,
    retry: retryUnlessClientError,
  });
}

/** Bindings change on a sync in the API, not here, so this refreshes only when the runtime
 *  page is opened — the same cadence as the list it sits under. */
export function useRuntimeProjects(id: number) {
  return useQuery({
    queryKey: keys.runtimeProjects(id),
    queryFn: () => api.listRuntimeProjects(id),
  });
}

/** The ready template builds in an E2B runtime's team, with their session verdicts: a live
 *  E2B read, made while a template is chosen and while the chosen build's verdict is shown.
 *  Fresh for a minute, and never retried on a refusal (no key yet, or E2B said no). */
export function useRuntimeTemplates(id: number, { enabled }: { enabled: boolean }) {
  return useQuery({
    queryKey: keys.runtimeTemplates(id),
    queryFn: () => api.listRuntimeTemplates(id),
    enabled,
    retry: retryUnlessClientError,
    staleTime: 60_000,
  });
}

/** Every write lands in the list: `credentials` is what the table's column and the detail's
 *  rows both read, so the list is the one cache to refresh. */
function useRuntimeMutation<TArgs, TResult>(fn: (args: TArgs) => Promise<TResult>) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => client.invalidateQueries({ queryKey: keys.runtimes }),
  });
}

export function useUpdateRuntime(id: number) {
  return useRuntimeMutation((body: RuntimePatch) => api.updateRuntime(id, body));
}

export function useDeleteRuntime(id: number) {
  return useRuntimeMutation(() => api.deleteRuntime(id));
}

export function usePutRuntimeCredential(id: number) {
  return useRuntimeMutation(({ name, value }: { name: string; value: string }) =>
    api.putRuntimeCredential(id, name, value),
  );
}

export function useDeleteRuntimeCredential(id: number) {
  return useRuntimeMutation((name: string) => api.deleteRuntimeCredential(id, name));
}

/** Asks the vendor now. Nothing is stored, so nothing is invalidated: the answer is the result. */
export function useTestRuntimeCredential(id: number) {
  return useMutation({ mutationFn: () => api.testRuntimeCredential(id) });
}

/** Starts the login; the result (URL, code, session) is the whole point, and nothing has been
 *  stored yet, so there is nothing to invalidate. */
export function useStartRuntimeLogin(id: number) {
  return useMutation({ mutationFn: () => api.startRuntimeLogin(id) });
}

/** The device flow is a person at another tab; three seconds is quick enough that the page
 *  answers within a breath of them finishing and slow enough not to hammer a local API. */
const LOGIN_POLL_MS = 3_000;

/**
 * Polls a device login until it settles. `sessionId` null means none has been started, and the
 * query stays idle; a 404 (the API restarted and lost the session) is a final answer, not a
 * transport fault, so it is not retried either.
 */
export function useRuntimeLoginStatus(id: number, sessionId: string | null) {
  return useQuery({
    queryKey: keys.runtimeLogin(id, sessionId ?? ""),
    queryFn: () => api.getRuntimeLoginStatus(id, sessionId as string),
    enabled: sessionId !== null,
    // An error is a final answer too: a 404 after the API restarted would otherwise be polled
    // every three seconds for as long as the page is open.
    refetchInterval: (query) =>
      query.state.status === "error" || (query.state.data && query.state.data.status !== "pending")
        ? false
        : LOGIN_POLL_MS,
    retry: retryUnlessClientError,
  });
}
