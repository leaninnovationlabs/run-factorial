import type { RequestResult } from "../../api/types";

/**
 * The sentence beside a request's button when the pass it woke has not landed — a failing
 * ticket's request wakes nothing, and a wait can run out. The chip reads as before, so without
 * this a second click files a second request. Landed, the refetch already shows the pass.
 */
export function requestNote(verb: string, result: RequestResult | undefined): string | null {
  if (!result || result.landed) return null;
  return `${verb.charAt(0).toUpperCase()}${verb.slice(1)} requested. This page updates when it takes effect.`;
}
