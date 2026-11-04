/** The assistant's conversations, its sends and stops, and the proposals a person decides. The
 *  conversation itself is followed by `useConversationStream`, not polled.
 *
 *  A write refreshes the list without waiting for it: `mutateAsync` waits on what `onSettled`
 *  returns, and a send must not wait on a slow read of the list. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { keys } from "./keys";

/** Most recent first: the API orders them by their last item. */
export function useConversations() {
  return useQuery({ queryKey: keys.conversations, queryFn: api.listConversations });
}

export function useCreateConversation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.createConversation(),
    onSettled: () => void client.invalidateQueries({ queryKey: keys.conversations }),
  });
}

export function useRenameConversation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, title }: { id: number; title: string }) => api.renameConversation(id, title),
    onSettled: () => void client.invalidateQueries({ queryKey: keys.conversations }),
  });
}

export function useDeleteConversation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.deleteConversation(id),
    onSettled: () => void client.invalidateQueries({ queryKey: keys.conversations }),
  });
}

/** The send moves the conversation to the top of the list. */
export function useSendMessage() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, text, page }: { id: number; text: string; page: string | null }) =>
      api.sendAssistantMessage(id, { text, page }),
    onSettled: () => void client.invalidateQueries({ queryKey: keys.conversations }),
  });
}

export function useCancelTurn() {
  return useMutation({ mutationFn: (turnId: number) => api.cancelAssistantTurn(turnId) });
}

export function useApproveProposal() {
  return useMutation({ mutationFn: (proposalId: number) => api.approveProposal(proposalId) });
}

export function useDeclineProposal() {
  return useMutation({ mutationFn: (proposalId: number) => api.declineProposal(proposalId) });
}

export function useAssistantDraft(itemId: number | null) {
  return useQuery({
    queryKey: keys.assistantDraft(itemId ?? 0),
    queryFn: () => api.getAssistantDraft(itemId ?? 0),
    enabled: itemId !== null,
    staleTime: Number.POSITIVE_INFINITY,
  });
}
