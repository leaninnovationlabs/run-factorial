/**
 * Where a project's tickets live on the web. GitHub is the default, so a caller that has not
 * loaded a ticket source still builds the URLs it always did.
 */

export interface ForgeHost {
  kind: "github" | "gitlab";
  /** Origin, no trailing slash. */
  origin: string;
}

const GITHUB: ForgeHost = { kind: "github", origin: "https://github.com" };

export function forgeHost(
  source: { kind: string } | null | undefined,
  settings: Record<string, unknown> | undefined,
): ForgeHost {
  if (source?.kind !== "gitlab") return GITHUB;
  const bag = settings?.gitlab;
  const raw =
    bag !== null && typeof bag === "object" && "base_url" in bag && typeof bag.base_url === "string"
      ? bag.base_url.trim()
      : "";
  return { kind: "gitlab", origin: (raw || "https://gitlab.com").replace(/\/$/, "") };
}

export function repoHref(repo: string, forge: ForgeHost = GITHUB): string {
  return `${forge.origin}/${repo}`;
}

export function issueHref(repo: string, number: string, forge: ForgeHost = GITHUB): string {
  if (forge.kind === "gitlab") return `${forge.origin}/${repo}/-/issues/${number}`;
  return `https://github.com/${repo}/issues/${number}`;
}

export function pullHref(repo: string, number: string, forge: ForgeHost = GITHUB): string {
  if (forge.kind === "gitlab") return `${forge.origin}/${repo}/-/merge_requests/${number}`;
  return `https://github.com/${repo}/pull/${number}`;
}

export function treeHref(repo: string, branchPath: string, forge: ForgeHost = GITHUB): string {
  if (forge.kind === "gitlab") return `${forge.origin}/${repo}/-/tree/${branchPath}`;
  return `https://github.com/${repo}/tree/${branchPath}`;
}

export function commitHref(repo: string, sha: string, forge: ForgeHost = GITHUB): string {
  if (forge.kind === "gitlab") return `${forge.origin}/${repo}/-/commit/${sha}`;
  return `https://github.com/${repo}/commit/${sha}`;
}

/** The word the forge uses for the request a gate waits on. */
export function pullNoun(forge: ForgeHost = GITHUB): "PR" | "MR" {
  return forge.kind === "gitlab" ? "MR" : "PR";
}

export function forgeName(forge: ForgeHost = GITHUB): "GitHub" | "GitLab" {
  return forge.kind === "gitlab" ? "GitLab" : "GitHub";
}
