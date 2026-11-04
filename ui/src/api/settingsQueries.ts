/** The assistant's settings and its write-only credential. The Test that runs one turn follows
 *  its conversation's stream, so it lives with the Settings page (`useConnectionTest`). */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { keys } from "./keys";
import type { AssistantSettings } from "./types";

export function useAssistantSettings() {
  return useQuery({ queryKey: keys.assistantSettings, queryFn: api.getAssistantSettings });
}

/** Whether the assistant is on and has a credential: the chat's read, for anyone who may use it. */
export function useAssistantReady() {
  return useQuery({ queryKey: keys.assistantReady, queryFn: api.getAssistantReady });
}

export function useSaveAssistantSettings() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: AssistantSettings) => api.saveAssistantSettings(body),
    onSettled: () => client.invalidateQueries({ queryKey: keys.assistantSettings }),
  });
}

/** `gcTime: 0`: the variables hold the typed credential, which must not outlive the request in
 *  the mutation cache. */
export function useSetAssistantCredential() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (value: string) => api.setAssistantCredential(value),
    gcTime: 0,
    onSettled: () => client.invalidateQueries({ queryKey: keys.assistantSettings }),
  });
}

export function useClearAssistantCredential() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.clearAssistantCredential(),
    onSettled: () => client.invalidateQueries({ queryKey: keys.assistantSettings }),
  });
}
