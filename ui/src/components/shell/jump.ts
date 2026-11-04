/**
 * What the search in the top bar can jump to: a ticket by its number or title, a project, a
 * runtime. Matched here, against rows the dashboard already reads (`GET /api/orgs/{org}/overview`,
 * `GET /api/orgs/{org}/runtimes`), so the search asks the API nothing of its own.
 */

import type { OverviewResponse, Runtime } from "../../api/types";

export type JumpKind = "ticket" | "project" | "runtime";

export type Jump = { kind: JumpKind; label: string; detail: string; to: string };

/** At most this many answers: a list longer than a glance is a list nobody arrows through. */
export const JUMP_LIMIT = 8;

const KIND_ORDER: Record<JumpKind, number> = { ticket: 0, project: 1, runtime: 2 };

function issueNumber(key: string): string {
  const hash = key.lastIndexOf("#");
  return hash === -1 ? key : key.slice(hash + 1);
}

/**
 * Every answer whose words hold every word of the query, ranked: whatever starts with the query
 * first, then the rest, each kind in a fixed order so the list does not reshuffle as a word is
 * finished. A ticket's text starts with its number, so a number lists its own ticket ahead of
 * the longer numbers that begin with it.
 */
export function jumpMatches(
  query: string,
  overview: OverviewResponse | undefined,
  runtimes: Runtime[] | undefined,
): Jump[] {
  const words = query.trim().toLowerCase().replace(/^#/, "").split(/\s+/).filter(Boolean);
  if (words.length === 0) return [];
  const projects = new Map((overview?.projects ?? []).map((project) => [project.id, project.name]));

  const candidates: (Jump & { text: string })[] = [
    ...(overview?.tickets ?? []).map((ticket) => {
      const number = issueNumber(ticket.external_key);
      return {
        kind: "ticket" as const,
        label: `#${number}${ticket.title ? ` ${ticket.title}` : ""}`,
        detail: projects.get(ticket.project_id) ?? ticket.external_key,
        to: `/tickets/${ticket.ticket_id}`,
        text: `${number} ${ticket.title ?? ""} ${ticket.external_key}`.toLowerCase(),
      };
    }),
    ...(overview?.projects ?? []).map((project) => ({
      kind: "project" as const,
      label: project.name,
      detail: "Project",
      to: `/projects/${project.id}`,
      text: project.name.toLowerCase(),
    })),
    ...(runtimes ?? []).map((runtime) => ({
      kind: "runtime" as const,
      label: runtime.name,
      detail: `Runtime · ${runtime.slug}`,
      to: `/runtimes/${runtime.slug}`,
      text: `${runtime.name} ${runtime.slug}`.toLowerCase(),
    })),
  ];

  const first = words[0] ?? "";
  return candidates
    .filter((candidate) => words.every((word) => candidate.text.includes(word)))
    .sort(
      (a, b) =>
        Number(b.text.startsWith(first)) - Number(a.text.startsWith(first)) ||
        KIND_ORDER[a.kind] - KIND_ORDER[b.kind] ||
        a.label.localeCompare(b.label),
    )
    .slice(0, JUMP_LIMIT)
    .map(({ kind, label, detail, to }) => ({ kind, label, detail, to }));
}
