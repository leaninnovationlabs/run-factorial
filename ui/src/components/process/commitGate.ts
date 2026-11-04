/** The commit button's gate, and which agents a commit left as stubs: pure, so each clause is
 *  tested against literal inputs. */

import type { Runtime, SelectedFile } from "../../api/types";
import { isolationOf } from "../runtimes/runtimes";

/**
 * How many stale files still block the commit. A stale file only counts while it is BOTH
 * part of the submission (`selected`) and not yet accepted (`confirmed`, from "Use anyway") —
 * an unticked file the operator will never send is not this commit's problem, even if it
 * drifted; the row's own stale badge is still how they'd notice if they later re-tick it.
 */
export function unconfirmedStaleCount(
  stale: Iterable<string>,
  selected: ReadonlySet<string>,
  confirmed: ReadonlySet<string>,
): number {
  let count = 0;
  for (const path of stale) {
    if (selected.has(path) && !confirmed.has(path)) count += 1;
  }
  return count;
}

export interface CommitButtonInputs {
  /** `text.trim()` truthy. */
  hasText: boolean;
  /** The validate request for the text on screen has not answered yet. */
  checkingConfig: boolean;
  /** It answered with an error rather than a verdict: nothing is checking, and nothing will
   *  until the text changes. */
  checkFailed: boolean;
  /** `preview.isFetching`, or the listing still loading while repo edits are held: until it
   *  lands, no held edit can be judged stale. */
  checkingRepo: boolean;
  /** `commit.isPending`. */
  committing: boolean;
  /** Nothing a commit would carry differs from the pin: the process text is the pinned text, no
   *  file is ticked or edited, and no agent the config names is missing its manifest. */
  unchanged: boolean;
  /** `verdict?.valid && verdictFor.current === text`. */
  validForText: boolean;
  /** A preview exists for the exact text on screen. The explorer no longer waits for a click to
   *  populate, so this gates the button rather than labelling a second stage: until the server has
   *  said which files a commit would write, there is no file set to submit. */
  previewed: boolean;
  /** `unconfirmedStaleCount` over the ticked files, plus every stale repo edit — a held repo edit
   *  is always part of the commit, so it always counts. */
  unconfirmedStale: number;
}

/**
 * `reason` is why the commit cannot go right now, or null when it can; the page writes it beside the
 * button, which stays focusable (decision 50). `isLoading` is the one state that is the button's
 * own doing — the request is out — so it is a spinner rather than a reason.
 */
export interface CommitButtonState {
  reason: string | null;
  isLoading: boolean;
}

export function commitButtonState(input: CommitButtonInputs): CommitButtonState {
  const {
    hasText,
    checkingConfig,
    checkFailed,
    checkingRepo,
    committing,
    unchanged,
    validForText,
    previewed,
    unconfirmedStale,
  } = input;
  if (committing) return { reason: null, isLoading: true };
  const reason = !hasText
    ? "Write or import a config first."
    : checkingConfig
      ? "Checking the config…"
      : checkFailed
        ? "Factorial couldn't check the config. Edit it to check again."
        : !validForText
          ? "Fix the errors below first."
          : checkingRepo || !previewed
            ? "Checking which files the commit would write…"
            : unchanged
              ? "Nothing to commit. Edit a file first."
              : unconfirmedStale > 0
                ? `${unconfirmedStale} file${unconfirmedStale === 1 ? "" : "s"} changed since you edited ${unconfirmedStale === 1 ? "it" : "them"}. Review ${unconfirmedStale === 1 ? "it" : "them"} before you commit.`
                : null;
  return { reason, isLoading: false };
}

/**
 * Which written agent manifests are comments-only stubs — derived from `ConfigCommitOut.written`
 * (ground truth for what the commit actually contains) crossed with the paths this client itself
 * submitted, never from a variant's `offered`/`default_on`: those
 * flags describe what the catalog could offer, and `admin.py` dumps `components` as an
 * unconditional catalog snapshot independent of the operator's selection. `assemble.py` writes
 * the stub template over exactly one path per agent — its manifest — when that path was not
 * among the submitted files, so a written manifest absent from `sent` is the one ground-truth
 * signal available here.
 */
export function stubbedManifests(written: string[], sent: SelectedFile[]): string[] {
  const submitted = new Set(sent.map((file) => file.path));
  return written.filter(
    (path) => /^\.factorial\/agents\/[^/]+\/agent\.yaml$/.test(path) && !submitted.has(path),
  );
}

/** The E2B runtimes among `slugs` that the project's allowlist does not name: a spawn onto one is
 *  refused. A host runtime is never gated, and an unknown slug is the validator's to report. */
export function unpermitted(slugs: string[], runtimes: Runtime[], allowlist: string[]): string[] {
  return slugs.filter(
    (slug) =>
      !allowlist.includes(slug) &&
      runtimes.some((rt) => rt.slug === slug && isolationOf(rt.details) === "e2b"),
  );
}
