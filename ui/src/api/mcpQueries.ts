/** MCP access: the tokens an MCP client signs in with, and the tools they reach. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { keys } from "./keys";
import type { McpTokenMintIn } from "./types";

/** Refetched when the window regains focus (the default), which is how a sign-in finished in
 *  another tab clears `reauth_required` here. */
export function useMcpTokens() {
  return useQuery({ queryKey: keys.mcpTokens, queryFn: api.listMcpTokens });
}

/** The catalogue changes only with a deploy. */
export function useMcpTools() {
  return useQuery({
    queryKey: keys.mcpTools,
    queryFn: api.listMcpTools,
    staleTime: 5 * 60_000,
  });
}

/** `gcTime: 0`: the result holds the raw token, so it goes the moment the page that asked for it
 *  unmounts, never kept in the cache for the default five minutes. */
export function useMintMcpToken() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: McpTokenMintIn) => api.mintMcpToken(body),
    gcTime: 0,
    onSettled: () => client.invalidateQueries({ queryKey: keys.mcpTokens }),
  });
}

export function useRevokeMcpToken() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.revokeMcpToken(id),
    onSettled: () => client.invalidateQueries({ queryKey: keys.mcpTokens }),
  });
}
