/**
 * Query states for tests that render against a seeded cache and cannot wait for a fetch.
 */

import type { QueryClient } from "@tanstack/react-query";
import { ApiError } from "./client";

/**
 * A query that has failed, as the cache holds one after its last attempt. `data` is what an
 * earlier fetch left, if any: TanStack keeps it through a failed refetch. Render with
 * `retryOnMount: false` so the first paint shows this state rather than a retry it never runs.
 */
export function seedFailure(
  qc: QueryClient,
  queryKey: readonly unknown[],
  message: string,
  data?: unknown,
  status = 503,
): void {
  const error = new ApiError(status, message);
  qc.getQueryCache()
    .build(qc, { queryKey })
    .setState({
      data,
      dataUpdateCount: data === undefined ? 0 : 1,
      dataUpdatedAt: data === undefined ? 0 : Date.parse("2026-08-25T10:42:00Z"),
      error,
      errorUpdateCount: 1,
      errorUpdatedAt: Date.parse("2026-08-25T10:43:00Z"),
      fetchFailureCount: 1,
      fetchFailureReason: error,
      fetchMeta: null,
      isInvalidated: false,
      status: "error",
      fetchStatus: "idle",
    });
}
