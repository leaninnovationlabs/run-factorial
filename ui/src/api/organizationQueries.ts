/** Organizations: the list the shell checks the page's slug against and names it from, and, with
 *  multi-org on, the page's members, invites and what the reader may do (design.md §7.19). */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useOrg } from "../org";
import { api } from "./client";
import { keys } from "./keys";

export function useOrganizations() {
  return useQuery({ queryKey: keys.organizations, queryFn: api.listOrganizations });
}

/** The organization this page acts in, once the list has loaded; undefined before then, and for
 *  a slug no organization carries. */
export function useCurrentOrganization() {
  const org = useOrg();
  const organizations = useOrganizations();
  return { ...organizations, organization: organizations.data?.find((o) => o.slug === org) };
}

/** What the deployment offers: its sign-in doors, whether strangers sign up, and whether it runs
 *  many organizations. The flag changes only with a restart, so it is read once a page load. */
export function useAuthProviders() {
  return useQuery({
    queryKey: keys.authProviders,
    queryFn: api.authProviders,
    staleTime: Infinity,
  });
}

export function useMultiOrg(): boolean {
  return useAuthProviders().data?.multi_org === true;
}

export function useOrganizationDetail() {
  return useQuery({ queryKey: keys.organization, queryFn: api.getOrganization });
}

/** Whether the reader holds `permission` here. False until the answer arrives, so an action the
 *  API might refuse is never offered on a guess; the API still decides. */
export function useCan(permission: string): boolean {
  return useOrganizationDetail().data?.permissions.includes(permission) ?? false;
}

export function useRoles() {
  return useQuery({ queryKey: keys.roles, queryFn: api.listRoles, staleTime: Infinity });
}

export function usePermissions() {
  return useQuery({
    queryKey: keys.permissions,
    queryFn: api.listPermissions,
    staleTime: Infinity,
  });
}

export function useMembers() {
  return useQuery({ queryKey: keys.members, queryFn: api.listMembers });
}

export function useInvites(enabled: boolean) {
  return useQuery({ queryKey: keys.invites, queryFn: api.listInvites, enabled });
}

/** `gcTime: 0`: the result holds the invite's link, a credential until it is used, so it goes
 *  when the form that showed it unmounts. */
export function useCreateInvite() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: api.createInvite,
    gcTime: 0,
    onSettled: () => client.invalidateQueries({ queryKey: keys.invites }),
  });
}

export function useRevokeInvite() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: api.revokeInvite,
    onSettled: () => client.invalidateQueries({ queryKey: keys.invites }),
  });
}

export function useChangeMemberRole() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, role }: { userId: number; role: string }) =>
      api.changeMemberRole(userId, role),
    onSettled: () => client.invalidateQueries({ queryKey: keys.members }),
  });
}

export function useRemoveMember() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: api.removeMember,
    onSettled: () => client.invalidateQueries({ queryKey: keys.members }),
  });
}
