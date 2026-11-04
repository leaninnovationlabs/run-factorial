/**
 * TanStack Query bindings. Polling is the transport (tech-stack §1; SSE deferred in §5).
 *
 * Intervals are chosen against the worker's own tick, `FACTORIAL_WORKER__POLL_INTERVAL_SECONDS`, which
 * defaults to 30s: polling much faster than the loop can change anything just burns requests.
 * Live views refresh at 10s so a change lands within a third of a tick; configuration data is
 * refetched on demand, because only this UI changes it.
 */

export * from "./assistantQueries";
export { keys } from "./keys";
export * from "./mcpQueries";
export * from "./processQueries";
export * from "./projectQueries";
export * from "./runtimeQueries";
export * from "./settingsQueries";
export * from "./ticketQueries";
