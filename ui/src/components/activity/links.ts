import type { TimelineEntry } from "../../api/types";
import { commitHref, type ForgeHost, issueHref, pullHref, pullNoun, treeHref } from "../../forge";
import { shortSha } from "../../format";
import { CONDITION_TONES } from "../../status";
import { issueNumber, str } from "./details";
import type { Held } from "./segments";

/** A GitHub thing an event names — the PR, issue, branch, or commit it is about. */
export interface EventLink {
  label: string;
  /**
   * Null when the row names a GitHub thing that cannot be addressed. A spawn records the branch
   * DIRECTIVE it gave the agent, and `{slug}` is the agent's to choose, so the name is still a
   * template until something is delivered — the label is worth showing, a tree URL built from it
   * is not (`worker/agent_steps.py`).
   */
  href: string | null;
}

/** Detail keys that name a commit, most specific first. */
const SHA_KEYS = ["merged_sha", "contract_sha", "sha"] as const;

/**
 * An ADLC template placeholder that nothing expanded — `config/templates.py`'s own grammar, so
 * this reads a `{slug}` the orchestrator left standing and not any brace a branch name may carry.
 */
const UNEXPANDED = /\{[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_-]+)?\}/;

/**
 * The GitHub thing an event is about, if it named one.
 *
 * Read off `details` rather than from a per-type table, because the same key means the same thing
 * wherever it appears: `worker/gates.py` writes `pr_number` on both gate decisions, the retired
 * dry-run mode wrote `issue_number` on every suppressed issue write, and an event type this build
 * has never heard of still gets a link if it carries one of these keys. Exactly one link per row — the
 * first match below — so a row never turns into a list of links.
 *
 * Nothing here is trusted enough to be pasted into a URL as-is: a branch name is agent-chosen
 * (`{slug}`) and a sha is only linked when it looks like one.
 */
export function linkFor(
  entry: TimelineEntry,
  repo: string | null | undefined,
  forge?: ForgeHost,
): EventLink | null {
  if (!repo) return null;
  const d = entry.details;

  // `dry_run_write_suppressed`, which only older rows carry, names its target by the argument the
  // projector call took: a suppressed `close_pull_request` says `number` and a suppressed
  // `delete_branch` says `name`. Both keys are too generic to read on any other event type.
  const verb = entry.type === "dry_run_write_suppressed" ? str(d.verb) : null;

  const pr =
    issueNumber(d.pr_number) ?? (verb === "close_pull_request" ? issueNumber(d.number) : null);
  if (pr) return { label: `${pullNoun(forge)} #${pr}`, href: pullHref(repo, pr, forge) };

  const issue = issueNumber(d.issue_number) ?? issueNumber(d.external_key);
  if (issue) return { label: `issue #${issue}`, href: issueHref(repo, issue, forge) };

  const branch = str(d.branch) ?? (verb === "delete_branch" ? str(d.name) : null);
  if (branch) {
    if (UNEXPANDED.test(branch)) return { label: branch, href: null };
    // Split on `/` first: a branch is a path in the tree URL, so encoding the separator away
    // would point every `spec/204-x` branch at a ref that does not exist.
    const path = branch.split("/").map(encodeURIComponent).join("/");
    return { label: branch, href: treeHref(repo, path, forge) };
  }

  for (const key of SHA_KEYS) {
    const sha = str(d[key]);
    if (sha && /^[0-9a-f]{7,40}$/i.test(sha)) {
      return { label: shortSha(sha), href: commitHref(repo, sha, forge) };
    }
  }
  return null;
}

/** The pull request a gate is waiting on: the newest `gate_waiting` event for `step` that names
 *  one. The page offers it as the one link a person at a gate needs. */
export function waitingGatePr(entries: readonly TimelineEntry[], step: string): string | null {
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    if (entry?.type === "gate_waiting" && str(entry.details.step) === step) {
      return issueNumber(entry.details.pr_number);
    }
  }
  return null;
}

/** Which pull request to show at a gate: live forge observation wins over the frozen event. */
export function gatePullNumber(
  live: number | null | undefined,
  entries: readonly TimelineEntry[],
  step: string,
): string | null {
  if (live != null) return String(live);
  return waitingGatePr(entries, step);
}

/** How a row's link is drawn, matching how the graph draws the held node. */
export type LinkPulse = "none" | "blink" | "error";

/**
 * Blink the link on the row that says why the ticket has stopped, and only that one.
 *
 * The graph blinks exactly one node, so the timeline blinks exactly one link and the page has one
 * thing moving on it. `held` is already narrowed to this row's block by the caller; `significant`
 * is what keeps a blocked block's `agent_spawned` branch link still while the escalation beside it
 * pulses — that row is bookkeeping, not the thing the reader is being sent to.
 *
 * An error takes no beat: `RowLink` draws it in the bold danger role with the error mark instead, as
 * the graph does. A ticket someone parked on purpose still blinks plainly.
 */
export function linkPulse(held: Held | null, significant: boolean): LinkPulse {
  if (held === null || !significant) return "none";
  return CONDITION_TONES[held.condition] === "error" ? "error" : "blink";
}
