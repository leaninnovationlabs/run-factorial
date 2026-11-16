/** A project's processes: syncs, pinned versions, the config explorer and the builder. */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { errorDetails, errorHint, errorText } from "../format";
import { api } from "./client";
import { keys } from "./keys";
import type { ConfigDelete, ConfigEdit, SeedSource, SelectedFile } from "./types";

export function useSyncProcessVersions(id: number) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (ref: string | null) => api.syncProcessVersions(id, ref),
    // The lanes offered, the admission reasons and the label vocabulary all follow the pinned
    // version, and all sit under the project's key. A refusal refreshes them too: the branch
    // may have moved since the page read it, and the readiness row is what offers the way out.
    onSettled: () => client.invalidateQueries({ queryKey: keys.project(id) }),
  });
}

/** Fetched only while the review is open: it costs a GitHub compare. */
export function useUnknownHead(id: number, enabled: boolean) {
  return useQuery({
    queryKey: keys.unknownHead(id),
    queryFn: () => api.getUnknownHead(id),
    enabled,
    staleTime: 0,
  });
}

export function useAdoptConfigHead(id: number) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (sha: string) => api.adoptConfigHead(id, sha),
    // Not awaited on success: the caller's onSuccess, which closes the review, waits for this
    // one, and a refetched preview would show its 409 "nothing to adopt" in the meantime.
    onSuccess: () => {
      client.removeQueries({ queryKey: keys.unknownHead(id) });
      void client.invalidateQueries({ queryKey: keys.project(id) });
    },
    onError: () => client.invalidateQueries({ queryKey: keys.project(id) }),
  });
}

/** Promote a lane to the project default, or clear it. Renames nothing and retires nothing:
 *  both lanes keep their version series (design.md §4). */
export function useSetDefaultProcess(id: number) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (processKey: string | null) => api.setDefaultProcess(id, processKey),
    // Which issues are admissible changes with the default, so the pending reasons (under the
    // project's key) are refreshed with it.
    onSuccess: () => client.invalidateQueries({ queryKey: keys.project(id) }),
  });
}

export function useProcessVersionDetail(id: number, versionId: number | undefined) {
  return useQuery({
    queryKey: keys.processVersion(id, versionId),
    queryFn: () => api.getProcessVersion(id, versionId as number),
    enabled: versionId !== undefined,
    staleTime: Infinity, // versions are immutable
  });
}

export function useProcessVersionRaw(id: number, versionId: number | undefined) {
  return useQuery({
    queryKey: keys.processVersionRaw(id, versionId),
    queryFn: () => api.getProcessVersionRaw(id, versionId as number),
    enabled: versionId !== undefined,
    staleTime: Infinity, // a version is immutable; its raw text cannot change
  });
}

/** Fetched only once a reader selects an agent step: each agent costs several GitHub reads. */
export function useProcessVersionAgents(
  id: number,
  versionId: number | undefined,
  enabled: boolean,
) {
  return useQuery({
    queryKey: keys.processVersionAgents(id, versionId),
    queryFn: () => api.listProcessVersionAgents(id, versionId as number),
    enabled: enabled && versionId !== undefined,
    staleTime: Infinity, // read at the version's pinned commit, which cannot change
  });
}

/** The pinned `.factorial/` listing. Immutable per (version, pin) apart from the drift column,
 *  which is read against a moving head — so this refetches on mount rather than caching for ever. */
export function useConfigFiles(id: number, versionId: number | undefined) {
  return useQuery({
    queryKey: keys.configFiles(id, versionId),
    queryFn: () => api.listConfigFiles(id, versionId as number),
    enabled: versionId !== undefined,
  });
}

/** One file's text at the pinned commit. A commit is immutable, so a hit can never be stale. */
export function useConfigFile(id: number, versionId: number | undefined, path: string | null) {
  return useQuery({
    queryKey: keys.configFile(id, versionId, path),
    queryFn: () => api.readConfigFile(id, versionId as number, path as string),
    enabled: versionId !== undefined && path !== null,
    staleTime: Infinity,
  });
}

export function useProjectLabels(id: number) {
  return useQuery({
    queryKey: keys.projectLabels(id),
    queryFn: () => api.listProjectLabels(id),
    retry: false, // a 409 (no config yet) is an answer, not a flake
  });
}

/** The gallery's "From <default branch>" card. Live GitHub reads, so only while that card can
 *  be offered: the branch is missing. */
export function useDefaultBranchConfig(id: number, enabled: boolean) {
  return useQuery({
    queryKey: keys.defaultBranchConfig(id),
    queryFn: () => api.getDefaultBranchConfig(id),
    enabled: enabled && id > 0,
  });
}

export function useProcessTemplates() {
  return useQuery({
    queryKey: keys.processTemplates,
    queryFn: api.listProcessTemplates,
    staleTime: Infinity, // the registry changes only with a deploy
  });
}

/** Commits exactly the files the operator assembled — the explorer's own selection, never a
 *  name-matched default — and syncs the commit, so it invalidates what a sync does. */
export function useCommitConfig(id: number) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: {
      content: string;
      files: SelectedFile[];
      edits: ConfigEdit[];
      created?: SelectedFile[];
      deleted?: ConfigDelete[];
    }) =>
      api.commitConfig(id, input.content, input.files, input.edits, input.created, input.deleted),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.project(id) }),
  });
}

/** Every synced snapshot, for the history. Fetched only while the history is open. */
export function useProcessVersions(id: number, enabled: boolean) {
  return useQuery({
    queryKey: keys.processVersions(id),
    queryFn: () => api.listProcessVersions(id),
    enabled,
  });
}

/** What a restore to this version would do, read against the head as it is now: never cached,
 *  because the head moves. */
export function useRestorePreview(id: number, versionId: number | null) {
  return useQuery({
    queryKey: keys.restorePreview(id, versionId),
    queryFn: () => api.getRestorePreview(id, versionId as number),
    enabled: versionId !== null,
    staleTime: 0,
    gcTime: 0,
    retry: false, // a 409 at an unknown head is an answer
  });
}

/** A restore is a commit and a sync, so it invalidates what those do. */
export function useRestoreConfig(id: number) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: { versionId: number; expectedHead: string }) =>
      api.restoreConfig(id, input.versionId, input.expectedHead),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.project(id) }),
  });
}

/** A commit's own checks on the draft, on demand. Nothing to invalidate: the verdict is the
 *  whole result, and the caller drops it when the draft changes. */
export function useCheckConfig(id: number) {
  return useMutation({
    mutationFn: (input: {
      content: string;
      files: SelectedFile[];
      edits: ConfigEdit[];
      created?: SelectedFile[];
      deleted?: ConfigDelete[];
    }) =>
      api.checkConfig(id, input.content, input.files, input.edits, input.created, input.deleted),
  });
}

/** An import that failed part-way, saying which processes landed before it stopped: each lands
 *  in its own commit, so the ones before the failure are on the branch and synced. */
export class ImportStopped extends Error {
  readonly imported: string[];
  readonly failedAt: string;
  /** The failed request's next step and technical text, so the error box still offers them. */
  readonly hint: string | null;
  readonly detail: string | null;

  constructor(imported: string[], failedAt: string, cause: unknown) {
    const reason = errorText(cause);
    super(
      imported.length > 0
        ? `Imported ${imported.join(", ")}; ${failedAt} was not imported: ${reason}`
        : `${failedAt} was not imported: ${reason}`,
      { cause },
    );
    this.imported = imported;
    this.failedAt = failedAt;
    this.hint = errorHint(cause);
    this.detail = errorDetails(cause);
  }
}

/**
 * Imports a template's lanes in one press: seeds the `factorial` branch first when asked, then
 * commits each lane with every file its proposal offers that the repo does not have yet — the
 * editor's own default — one commit each, in order. Each commit syncs, so a later lane is
 * proposed against what the earlier ones wrote: a skill two lanes share is written once. Stops
 * at the first failure, saying which landed.
 */
export function useImportProcesses(id: number) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: {
      seed: SeedSource | null;
      processes: { name: string; content: string }[];
    }) => {
      if (input.seed) await api.createConfigBranch(id, input.seed);
      const imported: string[] = [];
      for (const process of input.processes) {
        try {
          const proposal = await api.previewConfigBundle(id, process.content);
          const files = proposal.files
            .filter((file) => !file.present_in_repo)
            .map((file) => ({ path: file.path, body: file.body }));
          await api.commitConfig(id, process.content, files, []);
          imported.push(process.name);
        } catch (cause) {
          throw new ImportStopped(imported, process.name, cause);
        }
      }
      return imported;
    },
    // Not awaited: the refetches include live GitHub reads, and the section's next step — closing
    // the gallery, syncing a copied folder — must not wait on them.
    onSettled: () => {
      void client.invalidateQueries({ queryKey: keys.project(id) });
    },
  });
}

/**
 * What a commit from this text would create. Fetched on demand (`enabled`) rather than a
 * mutation: keying on `content` itself is what makes "a preview belongs to the exact text it was
 * run against" a fact of the cache rather than a ref the caller has to maintain by hand — edit
 * the text and the query for the NEW content simply has no cached answer yet, with no extra
 * bookkeeping to invalidate the old one. Until it has, the previous answer is shown as
 * placeholder data, and `isPlaceholderData` tells the caller it is not for this text.
 */
export function usePreviewConfigBundle(id: number, content: string, enabled: boolean) {
  return useQuery({
    queryKey: keys.configPreview(id, content),
    queryFn: () => api.previewConfigBundle(id, content),
    enabled,
    staleTime: Number.POSITIVE_INFINITY, // the repo state a preview reads is re-fetched by asking again, not by polling
    // The previous text's answer stays on screen while this one loads, so the explorer does not
    // empty on every edit; `isPlaceholderData` says the answer is not for this text.
    placeholderData: keepPreviousData,
  });
}

/** Pure check — nothing to invalidate, the verdict is the whole result. */
export function useValidateProcess(id: number) {
  return useMutation({
    mutationFn: (content: string) => api.validateProcess(id, content),
  });
}
