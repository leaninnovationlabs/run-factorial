/**
 * The configuration pages, and the readiness rows they draw — the vocabulary the side navigation,
 * the Project settings overview and each section's page share. Strings and pure functions only, so
 * the navigation, mounted on every page of a project, imports none of the sections' forms.
 *
 * The rows come off `project.readiness`, merged with the repo-side probes (`GET /readiness`):
 * the server's arrays are the geography, so a row's presence (`branch_protection` disappears
 * under `policy: ignored`) and state come from the same truth the activation check reads. The
 * sections are the pages that put those rows in a reading order, some of them several to a page:
 * Git Source draws every row about the repository host — the App's reach, the rules on the default
 * and config branches, the CI, the gate approvers — and Tickets draws the ticket source and its
 * GitHub connection with the labels its issues carry. The config branch itself, missing or on a
 * pushed head, is Process's.
 * Details has no row of its own, and Runtime adds the project's permits to its runtime rows.
 */

import type { ProjectDetail, ProjectReadiness, ReadinessItem, ReadinessKey } from "../../api/types";
import type { Role } from "../../status";
import type { Glyph } from "../ui";

export const ROW_TITLES: Record<ReadinessKey, string> = {
  app_access: "GitHub App access",
  github_connection: "GitHub connection",
  github_webhook: "GitHub webhook",
  config_branch: "Config branch",
  config_branch_rules: "Config branch rules",
  ticket_source: "Ticket source",
  process_config: "Process config",
  secrets: "Secrets",
  labels: "Ticket labels",
  branch_protection: "Branch protection",
  ci_workflows: "CI workflows",
  gate_approvers: "Gate approvers",
  runtime: "Runtime",
};

/**
 * A readiness state in words, colour and shape. Four states, four treatments: `unknown` looks
 * like neither a pass nor a failure (see `ReadinessItem` in `api/types.ts`), and `blocked` and
 * `warn` do not share a mark — one stops an activation, the other does not.
 */
export const READINESS: Record<
  ReadinessItem["state"],
  { label: string; appearance: Role; glyph: Glyph; ink: string }
> = {
  ok: { label: "Ready", appearance: "success", glyph: "status-success", ink: "text-icon-success" },
  blocked: {
    label: "Blocks activation",
    appearance: "danger",
    glyph: "status-error",
    ink: "text-icon-danger",
  },
  warn: {
    label: "Needs attention",
    appearance: "warning",
    glyph: "status-warning",
    ink: "text-icon-warning",
  },
  unknown: {
    label: "Not checked",
    appearance: "neutral",
    glyph: "status-information",
    ink: "text-icon-subtle",
  },
};

//: Rows the server reports but never counts against activation — say so where the chip is
//: read, or the shared vocabulary teaches the wrong lesson.
export const NEVER_BLOCKS = new Set<ReadinessKey>([
  "app_access",
  "github_webhook",
  "labels",
  "branch_protection",
  "config_branch_rules",
  "ci_workflows",
  "gate_approvers",
  "runtime",
]);

/** A row's state as the pages draw it. A row that never blocks activation can still be
 *  `blocked` (a runtime row: its lane cannot run; `app_access`: the App cannot see the repo), and calling that "Blocks activation" beside
 *  "Never blocks activation" contradicted itself — so it says what it does stop. */
export function readinessOf(key: string, state: ReadinessItem["state"]) {
  const readiness = READINESS[state];
  return state === "blocked" && NEVER_BLOCKS.has(key as ReadinessKey)
    ? { ...readiness, label: "Cannot run" }
    : readiness;
}

/** One configuration page. `keys` are the readiness rows it draws, in the order its body shows
 *  them — none for a page that has none; `probe` says one of them is a live GitHub read, which the
 *  page asks for on arrival. */
export type Section = {
  slug: SectionSlug;
  title: string;
  keys: readonly ReadinessKey[];
  /** Beside the title in the side navigation, from the glyphs the app already uses for the same
   *  things: Tickets', Runtimes' and MCP access's own. */
  icon: Glyph;
  probe: boolean;
  /** A page even before its rows answer, because its form is how they are fixed. */
  alwaysShown?: boolean;
  /** Rows the body says beside the field they are about, so the page's box leaves them out. */
  saidInBody?: readonly ReadinessKey[];
  /** Under the title in the overview, for a page whose rows do not say it. */
  summary?: string;
};

export type SectionSlug =
  | "details"
  | "repository-setup"
  | "process"
  | "ticket-source"
  | "runtime"
  | "secrets";

//: The reading order: the project, then the repository its work lands in, then the processes it
//: runs and the tickets they take, then how it runs. Presentational only — the server's arrays still own each row's
//: state and presence. The titles are the side navigation's, kept short; a message about one row
//: still names it in full (`ROW_TITLES`). The config branch has no page of its own: the process
//: page owns the branch the config lives on.
export const SECTIONS: readonly Section[] = [
  {
    slug: "details",
    title: "Details",
    keys: [],
    icon: "edit",
    probe: false,
    summary: "Name and description",
  },
  // Everything the repository host must carry, read live: the App's reach, the rules on the
  // default and config branches, the CI that `ci_green` waits on, and who may approve a gate.
  {
    slug: "repository-setup",
    title: "Git source",
    keys: [
      "app_access",
      "branch_protection",
      "config_branch_rules",
      "ci_workflows",
      "gate_approvers",
    ],
    icon: "branch",
    probe: true,
    alwaysShown: true,
    // The App's reach is a fact about the repository URL, so it is said under that field; the
    // branch rules check by check, each beside its own command.
    saidInBody: ["app_access", "branch_protection", "config_branch_rules"],
  },
  // The branch's row folds into this one (`mergeBranchRow`), and the branch is a probe.
  { slug: "process", title: "Process", keys: ["process_config"], icon: "lane", probe: true },
  // After Process: the processes decide which labels a ticket's issue needs, and the labels are
  // how a ticket's state shows on its issue, so they live with the tracker.
  {
    slug: "ticket-source",
    title: "Tickets",
    keys: ["ticket_source", "github_connection", "github_webhook", "labels"],
    icon: "tickets",
    probe: true,
    alwaysShown: true,
    saidInBody: ["github_connection", "github_webhook"],
  },
  // Which runtime each process runs on, and what this project permits them: the runtimes, the
  // extra hosts, the MCP servers. Permits are operator state rather than a readiness row, so the
  // page is one whether or not a process names a runtime.
  {
    slug: "runtime",
    title: "Runtime",
    keys: ["runtime"],
    icon: "runtimes",
    probe: false,
    alwaysShown: true,
    summary: "The runtimes, hosts and MCP servers this project may use",
  },
  { slug: "secrets", title: ROW_TITLES.secrets, keys: ["secrets"], icon: "key", probe: false },
];

export function sectionBySlug(slug: string | undefined): Section | null {
  return SECTIONS.find((section) => section.slug === slug) ?? null;
}

//: Pages that joined another; their addresses are still in links and histories.
//: A Map, because the slug comes from the address bar and an object would answer "constructor".
const RETIRED_SLUGS = new Map<string, SectionSlug>([
  ["app-access", "repository-setup"],
  ["branch-protection", "repository-setup"],
  ["ci-workflows", "repository-setup"],
  ["gate-approvers", "repository-setup"],
  ["labels", "ticket-source"],
  ["permits", "runtime"],
]);

/** The page a retired section's address now goes to, or null for any other slug. */
export function retiredSection(slug: string | undefined): SectionSlug | null {
  return (slug !== undefined && RETIRED_SLUGS.get(slug)) || null;
}

export function sectionForKey(key: ReadinessKey): Section | null {
  return SECTIONS.find((section) => section.keys.includes(key)) ?? null;
}

const WORST_LAST: ReadinessItem["state"][] = ["ok", "unknown", "warn", "blocked"];

/** Whether `a` is a worse state than `b`. */
export function isWorse(a: ReadinessItem["state"], b: ReadinessItem["state"]): boolean {
  return WORST_LAST.indexOf(a) > WORST_LAST.indexOf(b);
}

/** The server sends one `runtime` row per process that names a runtime; the pages draw them as
 *  one, which `RuntimeBody` breaks back out by process, under the worst of their states. */
function mergeRuntimeRows(rows: ReadinessItem[]): ReadinessItem[] {
  const runtimes = rows.filter((row) => row.key === "runtime");
  if (runtimes.length === 0) return rows;
  const worst = runtimes.reduce((a, b) =>
    WORST_LAST.indexOf(b.state) > WORST_LAST.indexOf(a.state) ? b : a,
  );
  const merged: ReadinessItem = {
    key: "runtime",
    process: null,
    runtime_slug: null,
    state: worst.state,
    summary:
      worst.state !== "ok"
        ? "Address runtime configuration"
        : runtimes.length === 1
          ? "Configured runtime ready"
          : "Configured runtimes ready",
    detail: null,
    remedy: null,
    link: null,
  };
  return [...rows.filter((row) => row.key !== "runtime"), merged];
}

export type BranchState = "checking" | "missing" | "unknown-head" | "not-checked" | "ok";

/** The `config_branch` row in the section's terms. `missing` and the `unknown head` prefix are
 *  the server's stable markers (readiness.py). The branch's rules are a row of their own, which
 *  the Git Source page draws beside the default branch's protection. */
export function branchState(row: ReadinessItem | undefined): BranchState {
  if (!row || row.summary === CHECKING) return "checking";
  if (row.state === "unknown") return "not-checked";
  if (row.summary === "missing") return "missing";
  if (row.summary.startsWith("unknown head")) return "unknown-head";
  return "ok";
}

/** Whether the branch notice already says why `process_config` is blocked, so the page leaves
 *  that row's own message out instead of speaking twice. */
export function saysHeadReview(branch: ReadinessItem | undefined): boolean {
  const state = branchState(branch);
  return state === "unknown-head" || state === "missing";
}

/** The branch's row folds into the process config row: one page owns the config and the branch
 *  it lives on. Only a warning carries over — an unchecked branch says nothing about the config. */
function mergeBranchRow(rows: ReadinessItem[]): ReadinessItem[] {
  const branch = rows.find((row) => row.key === "config_branch");
  const worse = branch !== undefined && (branch.state === "warn" || branch.state === "blocked");
  return rows.flatMap((row) => {
    if (row.key === "config_branch") return [];
    if (row.key !== "process_config" || !worse) return [row];
    return WORST_LAST.indexOf(branch.state) > WORST_LAST.indexOf(row.state)
      ? [{ ...row, state: branch.state }]
      : [row];
  });
}

/** The rows in the sections' reading order. Keys the server grows that no section lists yet
 *  render after these, in its own order. Exported because the dashboard's header sends the
 *  reader to the FIRST unfinished row, and "first" has to mean the same thing there as in the
 *  navigation. */
export function orderedRows(rows: ProjectDetail["readiness"]) {
  const order = SECTIONS.flatMap((section) => section.keys);
  const rank = (key: ReadinessKey) => {
    const index = order.indexOf(key);
    return index === -1 ? order.length : index;
  };
  return [...rows].sort((a, b) => rank(a.key) - rank(b.key));
}

//: The rows the probes always answer with. `branch_protection` and `ci_workflows` depend on the
//: process config, which the server reads for the page as `probe_keys`.
const ALWAYS_PROBED: ReadinessKey[] = [
  "app_access",
  "config_branch",
  "config_branch_rules",
  "labels",
];

/** What a probe row says before its answer: that it is on its way, or that nobody has asked. */
export const CHECKING = "Checking…";
export const UNCHECKED = "Not checked yet";

/** Stand-ins for the probe rows until they land, one for each row the server said will come. */
const placeholders = (keys: ReadinessKey[], summary: string): ReadinessItem[] =>
  keys.map((key) => ({
    key,
    process: null,
    runtime_slug: null,
    state: "unknown",
    summary,
    detail: null,
    remedy: null,
    link: null,
  }));

/** Every row the pages draw, from the project's own payload and the repo-side probes.
 *
 *  Two payloads because they cost different things: the first is a database read the page waits
 *  for, the second is live GitHub calls it must not. Holding every probe row's place keeps the
 *  navigation from rearranging under someone already reading it. `pending` is what a probe row
 *  says meanwhile: `CHECKING` while the probes run, `UNCHECKED` where nobody asked for them. */
export function ledgerRows(
  core: ReadinessItem[],
  probes: ProjectReadiness | undefined,
  probeKeys: ReadinessKey[] = ALWAYS_PROBED,
  pending: string = CHECKING,
): ReadinessItem[] {
  return orderedRows(
    mergeBranchRow(
      mergeRuntimeRows([...core, ...(probes?.readiness ?? placeholders(probeKeys, pending))]),
    ),
  );
}

/** The `config_branch` row as the server sent it, for the page that owns the branch. */
export function branchRow(
  core: ReadinessItem[],
  probes: ProjectReadiness | undefined,
  probeKeys: ReadinessKey[] = ALWAYS_PROBED,
  pending: string = CHECKING,
): ReadinessItem | undefined {
  return [...core, ...(probes?.readiness ?? placeholders(probeKeys, pending))].find(
    (row) => row.key === "config_branch",
  );
}

/** A page and the rows it draws. `row` is the one that speaks for the page — its worst, and of
 *  equals the first — or null for a page with none, or whose rows have not answered. */
export type SectionRow = { section: Section; row: ReadinessItem | null; rows: ReadinessItem[] };

/** The configuration pages this project has, each with the rows it draws: every section with a
 *  row the server sent or promised, Details, and those whose form is how their rows are fixed
 *  (`alwaysShown`). */
export function sectionRows(
  project: ProjectDetail,
  probes: ProjectReadiness | undefined,
  pending: string = CHECKING,
): SectionRow[] {
  const rows = ledgerRows(project.readiness, probes, project.probe_keys, pending);
  const byKey = new Map(rows.map((row) => [row.key, row]));
  return SECTIONS.flatMap((section): SectionRow[] => {
    const own = section.keys.flatMap((key) => byKey.get(key) ?? []);
    if (own.length === 0 && section.keys.length > 0 && !section.alwaysShown) return [];
    const row = own.reduce<ReadinessItem | null>(
      (worst, next) =>
        worst === null || WORST_LAST.indexOf(next.state) > WORST_LAST.indexOf(worst.state)
          ? next
          : worst,
      null,
    );
    return [{ section, row, rows: own }];
  });
}

/** The rows that stop an activation, in reading order, so the first is the first the reader
 *  will meet. `warn` rows are deliberately excluded — they never block. */
export function blockingRows(project: ProjectDetail): ReadinessItem[] {
  return orderedRows(project.readiness).filter((row) => row.state === "blocked");
}
