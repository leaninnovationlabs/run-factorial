/**
 * Where a project's tickets and code live on the web. Prefer the `forge` payload the API
 * sends; the ticket-source fallback keeps older payloads working.
 */

export interface ForgeHost {
  kind: string;
  origin: string;
  name?: string;
  pull_noun?: string;
  /** The request in words, where `pull_noun` is its abbreviation. */
  pull_name?: string;
  issue_path?: string;
  pull_path?: string;
  tree_path?: string;
  commit_path?: string;
}

const GITHUB: Required<ForgeHost> = {
  kind: "github",
  origin: "https://github.com",
  name: "GitHub",
  pull_noun: "PR",
  pull_name: "pull request",
  issue_path: "/issues/{key}",
  pull_path: "/pull/{number}",
  tree_path: "/tree/{branch}",
  commit_path: "/commit/{sha}",
};

const GITLAB: Required<ForgeHost> = {
  kind: "gitlab",
  origin: "https://gitlab.com",
  name: "GitLab",
  pull_noun: "MR",
  pull_name: "merge request",
  issue_path: "/-/issues/{key}",
  pull_path: "/-/merge_requests/{number}",
  tree_path: "/-/tree/{branch}",
  commit_path: "/-/commit/{sha}",
};

/** The built-in templates for a payload that predates the API's own. */
function defaults(forge: ForgeHost): Required<ForgeHost> {
  return forge.kind === "gitlab" ? GITLAB : GITHUB;
}

function format(template: string, values: Record<string, string>): string {
  return template.replace(/\{(\w+)\}/g, (_, name: string) => values[name] ?? "");
}

export function forgeHost(
  source: { kind: string } | null | undefined,
  settings: Record<string, unknown> | undefined,
  display?: ForgeHost | null,
): ForgeHost {
  if (display?.origin && display.issue_path) return display;
  if (source?.kind !== "gitlab") return GITHUB;
  const bag = settings?.gitlab;
  const raw =
    bag !== null && typeof bag === "object" && "base_url" in bag && typeof bag.base_url === "string"
      ? bag.base_url.trim()
      : "";
  return { ...GITLAB, origin: (raw || GITLAB.origin).replace(/\/$/, "") };
}

export function repoHref(repo: string, forge: ForgeHost = GITHUB): string {
  return `${forge.origin}/${repo}`;
}

export function issueHref(repo: string, number: string, forge: ForgeHost = GITHUB): string {
  const path = forge.issue_path ?? defaults(forge).issue_path;
  return `${forge.origin}/${repo}${format(path, { key: number })}`;
}

export function pullHref(repo: string, number: string, forge: ForgeHost = GITHUB): string {
  const path = forge.pull_path ?? defaults(forge).pull_path;
  return `${forge.origin}/${repo}${format(path, { number })}`;
}

export function treeHref(repo: string, branchPath: string, forge: ForgeHost = GITHUB): string {
  const path = forge.tree_path ?? defaults(forge).tree_path;
  return `${forge.origin}/${repo}${format(path, { branch: branchPath })}`;
}

export function commitHref(repo: string, sha: string, forge: ForgeHost = GITHUB): string {
  const path = forge.commit_path ?? defaults(forge).commit_path;
  return `${forge.origin}/${repo}${format(path, { sha })}`;
}

/** The word the forge uses for the request a gate waits on. */
export function pullNoun(forge: ForgeHost = GITHUB): string {
  return forge.pull_noun ?? defaults(forge).pull_noun;
}

export function pullName(forge: ForgeHost = GITHUB): string {
  return forge.pull_name ?? defaults(forge).pull_name;
}

export function forgeName(forge: ForgeHost = GITHUB): string {
  return forge.name ?? defaults(forge).name;
}
