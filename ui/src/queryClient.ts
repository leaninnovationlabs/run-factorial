import { MutationCache, QueryCache, QueryClient } from "@tanstack/react-query";
import { ApiError } from "./api/client";
import { showFlag } from "./components/ui/Flag";
import { errorText } from "./format";

function unauthorized(error: unknown): boolean {
  return error instanceof ApiError && error.unauthorized;
}

/**
 * The app's one query client.
 *
 * A 401 from any read or write goes to `onUnauthorized`: the token or the Google session has
 * lapsed, and signing in again is the only fix, so no panel has to recognise one. Retrying cannot
 * fix it either and only delays the sign-in screen; everything else gets one retry for a blip.
 *
 * Any other failed write is the page's to show, beside the control and in a flag
 * (`useActionFeedback`). The cache says it instead when no page can: the component that asked
 * unmounted before the answer came, because the reader moved on, and a write must never fail
 * unseen. Which mutations still have a mounted component is tracked from the cache's own
 * observer events, by observer, since a removal is reported even for one that was never added.
 */
export function createQueryClient(onUnauthorized: () => void): QueryClient {
  const watchers = new WeakMap<object, Set<object>>();
  const mutationCache = new MutationCache({
    onError: (error, _variables, _context, mutation) => {
      if (unauthorized(error)) return onUnauthorized();
      if (watchers.get(mutation)?.size) return;
      showFlag({
        appearance: "error",
        title: "A request failed after you left its page",
        description: errorText(error),
      });
    },
  });
  mutationCache.subscribe((event) => {
    if (event.type === "observerAdded") {
      const set = watchers.get(event.mutation) ?? new Set();
      watchers.set(event.mutation, set.add(event.observer));
    } else if (event.type === "observerRemoved" && !event.observer.hasListeners()) {
      // Only an unmounted component stops watching. A mounted one that sends a newer request is
      // also removed from the older, but it is still on the page, and an older request that
      // fails after a newer one replaced it is not news.
      watchers.get(event.mutation)?.delete(event.observer);
    }
  });
  return new QueryClient({
    queryCache: new QueryCache({
      onError: (error) => {
        if (unauthorized(error)) onUnauthorized();
      },
    }),
    mutationCache,
    defaultOptions: {
      queries: {
        retry: (failureCount, error) => !unauthorized(error) && failureCount < 1,
        staleTime: 5_000,
      },
    },
  });
}
