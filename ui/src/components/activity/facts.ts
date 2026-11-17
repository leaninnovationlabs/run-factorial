import type { TimelineEntry } from "../../api/types";
import { formatElapsed, shortSha } from "../../format";
import type { ConditionTone } from "../../status";
import { dueFrom, issueNumber, newest, str } from "./details";
import type { EventLink } from "./links";
import {
  COMPLETION_REFUSALS,
  CONFLICT_FIX_REFUSALS,
  changesRefused,
  EXIT_CAUSES,
  FRESH_SESSION_REASONS,
  HUMANISED,
  OUTPUT_LABELS,
  PAGE_LABELS,
  POSITIONAL,
  previewStop,
  REDO_REFUSALS,
  REFUSAL_REASONS,
  RESUME_REFUSALS,
  RETRY_REFUSALS,
  SURFACES,
  snippet,
  steering,
  viewClosed,
} from "./words";

export interface LongContent {
  title: string;
  body: string;
  /** The same link the row shows, carried into the modal so it is clickable there too. */
  link?: EventLink;
}

export interface EventFacts {
  /** Human label. `agent_completed` reads as "Agent completed", not as an enum member. */
  label: string;
  /** The single fact worth a row. Null means the label alone says everything. */
  primary: string | null;
  /**
   * When the attempt an event booked comes due, for the event types that book a future attempt.
   * The row counts down to it rather than printing it, so it is an ISO string here and not a
   * formatted one — the value changes every second and `factsFor` is computed once per poll.
   */
  dueAt?: string;
  /** Folded behind a click, because it has no bounded length. */
  long: LongContent | null;
  tone: ConditionTone;
  /**
   * Whether this event is why someone opened the page. `step_started` / `step_completed` bracket
   * the real events without being one; a failure or a gate decision is one. Drives emphasis, so
   * that scanning lands on the events that mean something.
   */
  significant: boolean;
}

/** A request's surface in the worker's words; null on a row without one, or with a label's. */
function surfaceOf(via: unknown): string | null {
  return SURFACES[str(via) ?? ""] ?? null;
}

/** " (admin)" when the reviewer counted by their repository role rather than by name. */
function roleOf(d: Record<string, unknown>): string {
  const role = str(d.permission);
  return str(d.qualified_as) === "permission" && role ? ` (${role})` : "";
}

/** Why a gate set a review aside, in the reviewer's terms. */
function ignoredWhy(d: Record<string, unknown>): string | null {
  switch (str(d.reason)) {
    case "app":
      return "the App's own review";
    case "bot":
      return "a bot not named in approvers";
    case "not_named":
      return "not named in approvers";
    case "below_permission": {
      const held = str(d.permission);
      return `${held && held !== "none" ? held : "no"} access, needs ${str(d.required) ?? "more"}`;
    }
    default:
      return str(d.reason);
  }
}

/**
 * A row that tells the reader how to release a parked ticket: a human interrupt, an idle park, a
 * session a person ended from its terminal, a session at its ceiling, or a rollover whose resume
 * was refused.
 */
function parks(entry: TimelineEntry): boolean {
  return (
    entry.type === "agent_interrupted" ||
    (entry.type === "agent_parked" &&
      (entry.details.reason === "idle" ||
        entry.details.reason === "exited" ||
        entry.details.reason === "ceiling" ||
        entry.details.reason === "rollover"))
  );
}

/**
 * Which rows of the block the ticket is stopped at are still true of it. One attempt can park
 * more than once — idle, released, then interrupted — and only the newest park is the one in
 * force: an earlier one's instruction names a label the issue no longer carries.
 */
export function currentRows(entries: readonly TimelineEntry[]): (entry: TimelineEntry) => boolean {
  // By id, not position: the panel hands the rows newest first by default.
  const park = newest(entries, parks);
  // A rollover resumes on its own tick; only one no resume followed is still holding the run.
  const resumed =
    park?.details.reason === "rollover" &&
    entries.some((entry) => entry.type === "agent_resumed" && entry.id > park.id);
  const newestPark = resumed ? undefined : park?.id;
  return (entry) => !parks(entry) || entry.id === newestPark;
}

const QUIET: ReadonlySet<ConditionTone> = new Set(["neutral", "active", "success"]);

/**
 * A row the activity's Focused switch leaves out: bookkeeping (`significant: false`) in a quiet
 * tone. A routine row that warns or fails, such as a preview that failed, stays, so Focused never
 * hides trouble.
 */
export function isRoutine(facts: Pick<EventFacts, "significant" | "tone">): boolean {
  return !facts.significant && QUIET.has(facts.tone);
}

/**
 * `current` marks the one block the ticket is stopped at (`heldAt`). Only an event that is still
 * true of the ticket may tell the reader to go and do something: the same `gate_waiting` read as
 * history is a record that someone was asked, not a second asking.
 */
export function factsFor(entry: TimelineEntry, current = false): EventFacts {
  const d = entry.details;
  const label = HUMANISED[entry.type] ?? entry.type.replace(/_/g, " ");

  switch (entry.type) {
    case "ticket_admitted":
      return {
        label,
        primary: str(d.owner) && `assigned to ${str(d.owner)}`,
        long: null,
        tone: "neutral",
        significant: true,
      };

    case "agent_spawned": {
      const agent = str(d.agent);
      // The slug, never `runtime`: that is the adapter kind, which names no runtime a reader can
      // open.
      const runtime = str(d.runtime_slug);
      const on = agent && runtime ? `${agent} on ${runtime}` : (agent ?? runtime);
      // A rework and a Retry both continue an earlier attempt's session.
      const from = d.resumed_from as { attempt?: unknown } | undefined;
      const continued =
        typeof from?.attempt === "number" ? `continued attempt ${from.attempt}` : null;
      return {
        label,
        primary: on && continued ? `${on}, ${continued}` : (on ?? continued),
        long: null,
        tone: "neutral",
        significant: false,
      };
    }

    case "admission_refused": {
      // No ticket exists, which IS the finding: an issue somebody meant for the factory never
      // entered it. `detail` carries the fix; the reason names the family.
      const reason = str(d.reason);
      const detail = str(d.detail);
      return {
        label,
        primary: reason ? (REFUSAL_REASONS[reason] ?? reason) : null,
        long: detail ? { title: "What to change", body: detail } : null,
        tone: "attention",
        significant: true,
      };
    }

    // Opened as the App for an MCP client (design.md §7.16). Project-scoped: a ticket, if one
    // follows, starts at `ticket_admitted`. The title and body stay on GitHub.
    case "issue_create_requested": {
      const by = str(d.requested_by);
      const via = surfaceOf(d.requested_via);
      return {
        label,
        primary: by && (via ? `by ${by} ${via}` : `by ${by}`),
        long: null,
        tone: "neutral",
        significant: true,
      };
    }

    case "issue_created": {
      const number = issueNumber(d.number);
      return {
        label,
        primary: number && `#${number}`,
        long: null,
        tone: "neutral",
        significant: true,
      };
    }

    case "agent_completed":
    case "shadow_accepted": {
      const summary = str(d.summary);
      return {
        label,
        primary: summary && snippet(summary),
        long: summary ? { title: "Agent summary", body: summary } : null,
        // A run a person forced past its failed conditions ended, but it did not succeed.
        tone: str(d.forced_by) ? "attention" : "success",
        significant: true,
      };
    }

    // `failure_summary` is the plain sentence; rows from before it existed fall back to the raw
    // reason, which stays the detail either way.
    case "agent_failed":
    case "action_failed":
    case "check_failed": {
      const reason = str(d.failure_reason);
      const summary = str(d.failure_summary) ?? reason;
      return {
        label,
        primary: summary && snippet(summary),
        long: reason ? { title: "Failure details", body: reason } : null,
        tone: "error",
        significant: true,
      };
    }

    case "postcondition_failed": {
      const needs = strings(d.needs);
      const failed = strings(d.failures);
      return {
        label,
        primary: needs[0] ?? failed[0] ?? null,
        long:
          needs.length || failed.length
            ? { title: "Expected results missing", body: neededBody(needs, failed) }
            : null,
        tone: "error",
        significant: true,
      };
    }

    case "retry_scheduled":
      return {
        label,
        primary: null,
        // `due_at`, which the worker emits (`worker/failures.py`) — not `backoff`, which it
        // never has. The row printed nothing at all for as long as it read the wrong key.
        dueAt: dueFrom(d.due_at),
        long: null,
        tone: "attention",
        significant: true,
      };

    case "gate_waiting":
      return {
        label,
        // Never the PR number: the row's own link already names it, on every gate row alike.
        primary: current ? "Review the pull request, then approve it or request changes" : null,
        long: null,
        tone: "attention",
        significant: true,
      };

    case "gate_approved":
      return {
        label,
        primary:
          [str(d.decided_by) && `by ${str(d.decided_by)}${roleOf(d)}`, d.merged ? "merged" : null]
            .filter(Boolean)
            .join(" · ") || null,
        long: null,
        tone: "success",
        significant: true,
      };

    // The approval stood and GitHub declined the merge, in its own words. Attention, not error:
    // the gate still waits, and the merge is tried again on every poll.
    case "gate_merge_refused": {
      const reason = str(d.reason);
      const by = str(d.approved_by);
      return {
        label,
        primary: reason ? snippet(reason) : by && `approved by ${by}`,
        long: reason ? { title: "Merge refused", body: reason } : null,
        tone: "attention",
        significant: true,
      };
    }

    // A review the gate weighed and set aside. Attention, not error: the gate still waits.
    case "gate_review_ignored": {
      const who = str(d.reviewer);
      const what = str(d.verdict) === "changes_requested" ? "changes requested" : "approval";
      const why = ignoredWhy(d);
      return {
        label,
        primary: who && `${what} by ${who}${why ? ` (${why})` : ""}`,
        long: null,
        tone: "attention",
        significant: true,
      };
    }

    // Attention, not error: the gate holds, and nothing was sent back (decision 79).
    case "gate_pr_closed":
      return {
        label,
        primary: "closed without merging; the gate holds",
        long: null,
        tone: "attention",
        significant: true,
      };

    case "gate_rejected": {
      const reason = str(d.reason);
      return {
        label,
        primary: reason && `${reason}${roleOf(d)}`,
        long: null,
        tone: "error",
        significant: true,
      };
    }

    case "escalated": {
      const reason = str(d.reason) ?? str(d.failure_reason);
      const summary = str(d.reason) ?? str(d.failure_summary) ?? reason;
      return {
        label,
        primary: summary && snippet(summary),
        long: reason ? { title: "Escalation", body: reason } : null,
        tone: "error",
        significant: true,
      };
    }

    // The runtime cannot run a session yet: the ticket waits, and the worker asks again by itself.
    case "spawn_refused": {
      const refusal = str(d.refusal);
      return {
        label,
        primary: refusal && snippet(refusal),
        long: refusal ? { title: "Why the runtime refused", body: refusal } : null,
        tone: "attention",
        significant: true,
      };
    }

    // A start held by the Factorial-managed key's limit: written once per entry of the step, and
    // again when the scope, the limit or the held attempt changes.
    case "sandbox_limit_waiting": {
      const limit = typeof d.limit === "number" ? d.limit : null;
      return {
        label,
        primary:
          limit === 0
            ? "not available to this organization"
            : d.scope === "pool"
              ? "the shared pool is full"
              : `limit of ${limit ?? "?"} for this organization`,
        long: null,
        tone: "attention",
        significant: true,
      };
    }

    // Not step-scoped (the tick died before it knew which step it was on), unlike every other
    // failure row above — see `segmentsFrom`, which falls back to filing it under "unknown".
    case "ticket_quarantined": {
      const error = str(d.error);
      return {
        label,
        primary: error && snippet(error),
        long: error ? { title: "Error details", body: error } : null,
        tone: "error",
        significant: true,
      };
    }

    // A spawn that could not reach the sandbox service: the ticket waits, the worker asks again on
    // every pass, and it is written once per outage. The exception is for whoever runs the service.
    case "substrate_unavailable": {
      const error = str(d.error);
      return {
        label,
        primary: "The sandbox service couldn't be reached. Factorial keeps trying.",
        long: error ? { title: "Error details", body: error } : null,
        tone: "attention",
        significant: true,
      };
    }

    // The answer to the row above, and the only operator gesture the API performs rather than
    // requests — so it is neutral rather than attention: nothing is pending and nothing is wrong.
    case "ticket_requeued": {
      const via = surfaceOf(d.requested_via);
      const failures = str(d.failures);
      return {
        label,
        primary: via && failures ? `${via}, after ${failures} failed checks` : via,
        long: null,
        tone: "neutral",
        significant: true,
      };
    }

    case "action_executed":
      return {
        label,
        primary: str(d.action) ?? str(d.contract_sha),
        long: null,
        tone: "success",
        significant: true,
      };

    // The three halves of a human interrupt (decision 26). All amber and all significant: these
    // are the rows that say why a parked ticket is parked, and the header pill, the graph node and
    // the attempt tag above them already read amber for the same reason.
    case "interrupt_requested":
      return {
        label,
        primary: surfaceOf(d.requested_via),
        long: null,
        tone: "attention",
        significant: true,
      };

    case "interrupt_overtaken":
      return {
        label,
        // The reason, not the outcome: "not applied" alone would leave the reader looking for a
        // fault. Nothing to act on, so no `run_id` here either.
        primary:
          d.reason === "parked" ? "the run was already paused" : "the run had already finished",
        long: null,
        tone: "attention",
        significant: true,
      };

    case "agent_interrupted":
      return {
        label,
        // What to do about it, and only while it is still true of the ticket — the rule
        // `gate_waiting` follows. Read as history this is a record that a run was stopped, and an
        // instruction on a settled attempt is an instruction to do something twice.
        // Deliberately not `run_id`: it names nothing a reader can act on.
        primary: current
          ? "To resume, remove the factorial:interrupted label from the issue"
          : null,
        long: null,
        tone: "attention",
        significant: true,
      };

    case "agent_parked":
      // An idle park waits on a person and is released like an interrupt, under its own label. A
      // rollover resumes on the same tick and asks nothing of anyone, unless that resume was
      // refused and it still holds the run. The terminal's Resume releases either, as the park's
      // comment says.
      if (d.reason === "idle") {
        return {
          label,
          primary: current
            ? "To resume, press Resume or remove the factorial:waiting label from the issue"
            : null,
          long: null,
          tone: "attention",
          significant: true,
        };
      }
      if (d.reason === "exited") {
        return {
          label,
          primary: current
            ? "Ended from its terminal. To resume, press Resume or remove the factorial:interrupted label from the issue"
            : "Ended from its terminal",
          long: null,
          tone: current ? "attention" : "neutral",
          significant: true,
        };
      }
      if (d.reason === "ceiling") {
        // Read as history too: the session stopped on the deployment's limit, not on anyone's say.
        // Only Resume releases it: any token in the repository can remove a label.
        const ran =
          typeof d.live_seconds === "number"
            ? `Paused at the session time limit (${formatElapsed(d.live_seconds)})`
            : "Paused at the session time limit";
        return {
          label,
          primary: current ? `${ran}. Press Resume to continue` : ran,
          long: null,
          tone: current ? "attention" : "neutral",
          significant: true,
        };
      }
      return current
        ? {
            label,
            primary:
              "To resume, press Resume or remove the factorial:interrupted label from the issue",
            long: null,
            tone: "attention",
            significant: true,
          }
        : { label, primary: null, long: null, tone: "neutral", significant: false };

    case "agent_resumed": {
      const steered = steering(d.comment_count, d.steered_by);
      // An operator's Resume may carry an answer and no comment: "no new instructions" would be
      // wrong about it, and the answer itself stays off the page.
      const comments = typeof d.comment_count === "number" ? d.comment_count : 0;
      const via = surfaceOf(d.via);
      return {
        label,
        primary: via ? (comments > 0 ? `${via}, ${steered}` : via) : steered,
        long: null,
        tone: "attention",
        significant: true,
      };
    }

    // The answer stays on the event for the loop to read, and off the page: it is free text an
    // operator typed, and the resumed turn is where it belongs.
    case "session_resume_requested": {
      const by = str(d.by);
      const answered = d.has_answer === true ? ", with an answer" : "";
      return {
        label,
        primary: by && `by ${by}${answered}`,
        long: null,
        tone: "attention",
        significant: true,
      };
    }

    case "session_resume_refused": {
      const reason = str(d.reason);
      return {
        label,
        primary: reason && (RESUME_REFUSALS[reason] ?? reason),
        long: null,
        tone: "attention",
        significant: true,
      };
    }

    case "session_resume_skipped": {
      const reason = str(d.reason);
      return {
        label,
        primary: reason && (FRESH_SESSION_REASONS[reason] ?? reason),
        long: null,
        tone: "neutral",
        significant: false,
      };
    }

    // The gateway's audit (decision 57): who watched or typed and how much, never what. None of
    // it is why anyone opened the page.
    case "terminal_attached": {
      const by = str(d.by);
      const doing = d.writable === true ? "typing" : "watching";
      const where = d.target === "inspect" ? " in an open session" : "";
      return {
        label,
        primary: by && `${by}, ${doing}${where}`,
        long: null,
        tone: "neutral",
        significant: false,
      };
    }

    case "terminal_detached": {
      const by = str(d.by);
      const seconds = typeof d.seconds === "number" ? d.seconds : null;
      return {
        label,
        primary: by && (seconds === null ? by : `${by} after ${formatElapsed(seconds)}`),
        long: null,
        tone: "neutral",
        significant: false,
      };
    }

    case "terminal_input": {
      const by = str(d.by);
      const bytes = typeof d.bytes === "number" ? d.bytes : null;
      return {
        label,
        primary:
          bytes === null
            ? by
            : `${bytes} ${bytes === 1 ? "byte" : "bytes"}${by ? ` by ${by}` : ""}`,
        long: null,
        tone: "neutral",
        significant: false,
      };
    }

    case "terminal_seat_taken": {
      const by = str(d.by);
      const from = str(d.from);
      return {
        label,
        primary: by && (from ? `${by} took over typing from ${from}` : `${by} started typing`),
        long: null,
        tone: "neutral",
        significant: false,
      };
    }

    case "inspect_requested":
    case "inspect_started":
    case "inspect_close_requested": {
      const by = str(d.requested_by);
      return { label, primary: by && `by ${by}`, long: null, tone: "neutral", significant: false };
    }

    case "inspect_parked": {
      const reason = str(d.reason);
      return {
        label: d.failed === true ? "Session failed" : label,
        primary: reason && viewClosed(reason, d),
        long: null,
        tone: d.failed === true ? "attention" : "neutral",
        significant: false,
      };
    }

    // Exit and re-entry (decision 29). Neutral, not amber: an exited ticket asks nothing of
    // anyone, which is the point of sending it away. Only a refusal is a request for a person.
    case "exit_requested":
    case "reentry_requested":
      return {
        label,
        primary:
          [surfaceOf(d.requested_via), d.close_issue === true ? "closing the issue" : null]
            .filter(Boolean)
            .join(", ") || null,
        long: null,
        tone: "neutral",
        significant: true,
      };

    case "ticket_exited":
      return {
        label,
        primary: EXIT_CAUSES[str(d.via) ?? ""] ?? null,
        long: null,
        tone: "neutral",
        significant: true,
      };

    case "ticket_reentered":
      return {
        label,
        primary: d.resumed === true ? "continued the interrupted session" : null,
        // The same countdown `unblocked` gets, for the same wait.
        dueAt: dueFrom(d.due_at),
        long: null,
        tone: "neutral",
        significant: true,
      };

    case "reentry_refused":
    case "unblock_refused":
      return {
        label,
        primary: REFUSAL_REASONS[str(d.reason) ?? ""] ?? null,
        long: null,
        tone: "attention",
        significant: true,
      };

    // A restart (#321): the request, the worker's answer, or its refusal. The answer has a block
    // of its own on the entry node (`segmentsFrom`), so it reads as the boundary it is.
    case "restart_requested": {
      const to = str(d.target_sha);
      return {
        label,
        primary:
          [to && `onto ${shortSha(to)}`, surfaceOf(d.requested_via)].filter(Boolean).join(", ") ||
          null,
        long: null,
        tone: "neutral",
        significant: true,
      };
    }

    case "ticket_restarted": {
      const lane = str(d.process);
      const to = str(d.to_sha);
      const from = str(d.from_sha);
      const by = str(d.requested_by);
      const via = surfaceOf(d.requested_via);
      const onto = to
        ? [
            lane ? `on ${lane} @ ${shortSha(to)}` : `on ${shortSha(to)}`,
            from && `(was ${shortSha(from)})`,
          ]
            .filter(Boolean)
            .join(" ")
        : null;
      const removed = restartRemoved(d);
      return {
        label,
        primary:
          [onto, by && (via ? `by ${by} ${via}` : `by ${by}`)].filter(Boolean).join(" · ") || null,
        long: removed ? { title: "What the restart removed", body: removed } : null,
        tone: "neutral",
        significant: true,
      };
    }

    case "restart_refused": {
      const detail = str(d.detail);
      return {
        label,
        primary: REFUSAL_REASONS[str(d.reason) ?? ""] ?? null,
        long: detail ? { title: "Why the restart was refused", body: detail } : null,
        tone: "attention",
        significant: true,
      };
    }

    // A redo (decision 76): the request, the worker's answer, or its refusal. The answer has a
    // block of its own on the step it starts again (`segmentsFrom`), as a restart's does. The
    // note is the person's words to the agent, shown as text and nowhere else.
    case "redo_requested": {
      const target = str(d.target_step);
      const note = str(d.note);
      return {
        label,
        primary:
          [target && `of ${target}`, surfaceOf(d.requested_via)].filter(Boolean).join(", ") || null,
        long: note ? { title: "Note for the fresh agent", body: note } : null,
        tone: "neutral",
        significant: true,
      };
    }

    case "step_redone": {
      const target = str(d.target_step);
      const by = str(d.requested_by);
      const removed = redoRemoved(d);
      return {
        label,
        primary: [target && `from ${target}`, by && `by ${by}`].filter(Boolean).join(" ") || null,
        long: removed ? { title: "What the redo did", body: removed } : null,
        tone: "neutral",
        significant: true,
      };
    }

    case "redo_refused": {
      const reason = str(d.reason) ?? "";
      const detail = str(d.detail);
      return {
        label,
        primary: REDO_REFUSALS[reason] ?? (reason || null),
        long: detail ? { title: "Why the redo was refused", body: detail } : null,
        tone: "attention",
        significant: true,
      };
    }

    // Not significant: a URL arriving says nothing about whether the step worked, and the
    // Preview button on the block header is the way to it.
    case "preview_url_updated": {
      const urls: unknown[] = Array.isArray(d.urls) ? d.urls : [];
      const first = urls.find(
        (entry): entry is { url: string } =>
          typeof entry === "object" &&
          entry !== null &&
          typeof (entry as { url?: unknown }).url === "string",
      );
      // A URL recorded before a run could have several services.
      const url = first?.url ?? str(d.url);
      return {
        label: url ? label : "Preview withdrawn",
        primary: url && hostOf(url),
        long: null,
        tone: "neutral",
        significant: false,
      };
    }

    case "preview_requested":
    case "preview_stop_requested":
    case "preview_kept": {
      const by = str(d.requested_by);
      return {
        label,
        primary: by && `by ${by}`,
        long: null,
        tone: "neutral",
        significant: false,
      };
    }

    case "preview_started":
    case "preview_stopped": {
      const reason = str(d.reason);
      const stop = reason === null ? null : previewStop(reason);
      return {
        label,
        primary: stop?.said ?? null,
        long: stop?.more ? { title: "Why the preview stopped", body: stop.more } : null,
        tone: "neutral",
        significant: false,
      };
    }

    // The reason can quote the sandbox's own output; it is folded, and shown as text.
    case "preview_failed": {
      const reason = str(d.reason);
      return {
        label,
        primary: reason?.split("\n")[0] ?? null,
        long: reason ? { title: "Why the preview failed", body: reason } : null,
        tone: "attention",
        significant: false,
      };
    }

    // Not significant, like the page: the Files button is the way to them. Without this case
    // the default would print the files array.
    case "run_outputs_observed": {
      const status = str(d.status);
      const files = Array.isArray(d.files) ? d.files.length : 0;
      return {
        label: OUTPUT_LABELS[status ?? ""] ?? label,
        primary:
          status === "observed"
            ? `${files} ${files === 1 ? "file" : "files"}`
            : status === "lookup_failed"
              ? str(d.reason)
              : status === "no_branch"
                ? str(d.branch)
                : null,
        long: null,
        tone: status === "ambiguous" || status === "lookup_failed" ? "attention" : "neutral",
        significant: false,
      };
    }

    // Recorded only while `preview.page` existed; the log keeps those, so the activity still
    // words them. Not significant, like a URL arriving.
    case "preview_page_observed": {
      const status = str(d.status);
      const commit = str(d.commit);
      return {
        label: PAGE_LABELS[status ?? ""] ?? label,
        primary:
          status === "published"
            ? commit
              ? shortSha(commit)
              : null
            : status === "lookup_failed"
              ? str(d.reason)
              : null,
        long: null,
        tone: status === "ambiguous" || status === "lookup_failed" ? "attention" : "neutral",
        significant: false,
      };
    }

    // Filed under the gate's block: the request is against the review, and the rejection that
    // answers it lands beside it. The feedback is folded, like a review comment.
    case "retry_requested": {
      const by = str(d.requested_by);
      return {
        label,
        primary: by && `by ${by}`,
        long: null,
        tone: "attention",
        significant: true,
      };
    }

    case "step_changes_requested": {
      const feedback = str(d.feedback);
      const by = str(d.requested_by);
      return {
        label,
        primary: by && `by ${by}`,
        long: feedback ? { title: "Requested changes", body: feedback } : null,
        tone: "attention",
        significant: true,
      };
    }

    case "completion_requested":
    case "step_accepted_on_request": {
      const by = str(d.requested_by);
      return {
        label,
        primary: by && `by ${by}`,
        long: null,
        tone: entry.type === "completion_requested" ? "attention" : "success",
        significant: true,
      };
    }

    case "completion_refused": {
      const reason = str(d.reason) ?? "";
      const failed = strings(d.failures);
      return {
        label,
        primary: COMPLETION_REFUSALS[reason] ?? (reason || null),
        long: failed.length ? { title: "Expected results not met", body: failed.join("\n") } : null,
        tone: "attention",
        significant: true,
      };
    }

    case "step_forced": {
      const by = str(d.requested_by);
      const failed = strings(d.failures);
      return {
        label,
        primary: by && `by ${by}`,
        long: failed.length ? { title: "Expected results not met", body: failed.join("\n") } : null,
        tone: "attention",
        significant: true,
      };
    }

    case "step_approved": {
      const by = str(d.approved_by);
      const head = str(d.head_sha);
      return {
        label,
        primary:
          [by && `by ${by}`, head && `at ${shortSha(head)}`].filter(Boolean).join(" ") || null,
        long: null,
        tone: "success",
        significant: true,
      };
    }

    case "step_retry_requested": {
      const by = str(d.requested_by);
      return {
        label,
        primary: by && `by ${by}`,
        long: null,
        tone: "attention",
        significant: true,
      };
    }

    case "step_retry_refused": {
      const reason = str(d.reason) ?? "";
      const needs = strings(d.needs);
      const unmet = needs.length ? needs : strings(d.unmet);
      const why = RETRY_REFUSALS[reason] ?? (reason || null);
      const detail = str(d.detail);
      return {
        label,
        primary: why && unmet.length ? `${why}: ${unmet.join("; ")}` : why,
        long: detail ? { title: "Why the attempt did not start", body: detail } : null,
        tone: "attention",
        significant: true,
      };
    }

    case "next_step_waiting": {
      const needs = strings(d.needs);
      const unmet = strings(d.unmet);
      return {
        label,
        primary: needs[0] ?? unmet[0] ?? null,
        long:
          needs.length || unmet.length
            ? { title: "Waiting for", body: neededBody(needs, unmet) }
            : null,
        tone: "attention",
        significant: true,
      };
    }

    case "step_changes_refused": {
      const reason = str(d.reason);
      return {
        label,
        primary: reason && changesRefused(reason, str(d.step), str(d.reviewed_step)),
        long: null,
        tone: "attention",
        significant: true,
      };
    }

    case "gate_mergeability": {
      const state = str(d.state);
      const base = str(d.base_ref) ?? "its target";
      return {
        label:
          state === "conflicting"
            ? `Pull request conflicts with ${base}`
            : state === "behind"
              ? `Pull request is behind ${base}`
              : state === "clean"
                ? "Pull request can merge again"
                : label,
        primary: null,
        long: null,
        tone: state === "clean" ? "success" : "attention",
        significant: true,
      };
    }

    case "conflict_fix_requested": {
      const by = str(d.requested_by);
      return {
        label,
        primary: by && `by ${by}`,
        long: null,
        tone: "attention",
        significant: true,
      };
    }

    case "conflict_fix_refused": {
      const reason = str(d.reason);
      return {
        label,
        primary: reason && (CONFLICT_FIX_REFUSALS[reason] ?? reason),
        long: null,
        tone: "attention",
        significant: true,
      };
    }

    case "gate_conflict_sent_back": {
      const target = str(d.target_step);
      const by = str(d.requested_by);
      return {
        label,
        primary:
          [
            target && `to ${target}`,
            d.request_id == null ? "started by the gate" : by && `by ${by}`,
          ]
            .filter(Boolean)
            .join(", ") || null,
        long: null,
        tone: "attention",
        significant: true,
      };
    }

    case "step_entered": {
      const from = str(d.from_step);
      const reason = str(d.reason);
      return {
        label,
        // Where the ticket came FROM, because `reject_rerun`, `restart` and `redo` run the graph
        // backward and the entered step's name alone reads as an ordinary advance.
        primary:
          reason === "restart"
            ? `restart${from ? `, from ${from}` : ""}`
            : reason === "redo"
              ? `redone${from ? `, from ${from}` : ""}`
              : reason === "conflict_fix"
                ? `conflict fix${from ? `, sent back from ${from}` : ""}`
                : reason === "conflict_fixed"
                  ? `conflict fixed${from ? `, from ${from}` : ""}`
                  : from &&
                    (reason === "reject_rerun" ? `rework, sent back from ${from}` : `from ${from}`),
        long: null,
        tone: "neutral",
        // Bracketing, like the two below: it says the ticket moved, never why anyone should care.
        significant: false,
      };
    }

    case "step_started":
    case "step_completed":
      return { label, primary: null, long: null, tone: "neutral", significant: false };

    case "ticket_done":
      return { label, primary: null, long: null, tone: "success", significant: true };

    // Amber, like the `unblocked` that answers it: a person is releasing an escalation.
    case "unblock_requested":
      return {
        label,
        primary: surfaceOf(d.requested_via),
        long: null,
        tone: "attention",
        significant: true,
      };

    case "unblocked":
      return {
        label,
        primary: null,
        // The same countdown a retry gets, because it is the same wait: the granted attempt is
        // still gated on `_retry_ready`, which reads the failed run's own backoff. Null wherever
        // that wait is not a clock — a gate, or an unmet precondition
        // (`worker/reconcile.py::_grant_attempt`).
        dueAt: dueFrom(d.due_at),
        long: null,
        tone: "attention",
        significant: true,
      };

    case "webhook_received":
      return {
        label,
        primary: webhookSummary(d),
        long: null,
        tone: "neutral",
        significant: false,
      };

    default: {
      // An unknown type still renders. `event.type` is TEXT with no CHECK precisely so the log can
      // record a type an older deployment does not know (`events/types.py`), and this is the
      // reader that must not fall over when it meets one.
      const first = Object.entries(d).find(([key]) => !POSITIONAL.has(key));
      const value = first ? (str(first[1]) ?? JSON.stringify(first[1])) : null;
      return {
        label,
        primary: value && `${first?.[0]}: ${snippet(value, 60)}`,
        long: null,
        tone: "neutral",
        significant: false,
      };
    }
  }
}

const WEBHOOK_SUBJECTS: Record<string, string> = {
  issues: "Issue",
  issue_comment: "Comment",
  pull_request: "Pull request",
  pull_request_review: "Review",
  pull_request_review_comment: "Review comment",
  push: "Push",
  check_suite: "Check suite",
};

/** "Pull request closed, merged · #42 · by operator", from what the receiver kept of the
 *  delivery (`api/routes/webhooks.py::_facts`). A row from before it kept any of that shows
 *  the bare event name. */
function webhookSummary(d: Record<string, unknown>): string | null {
  const event = str(d.github_event);
  if (!event) return null;
  const words = (value: unknown) => str(value)?.replaceAll("_", " ");
  const subject = WEBHOOK_SUBJECTS[event] ?? event;
  const action = words(d.action);
  const on =
    event === "issue_comment" ? ` on ${d.on_pull_request === true ? "pull request" : "issue"}` : "";
  const outcome = [
    d.merged === true ? "merged" : null,
    words(d.review_state),
    words(d.conclusion),
    str(d.label),
  ].filter((part): part is string => Boolean(part));
  const head = [subject, action].filter(Boolean).join(" ") + on;
  const number = typeof d.number === "number" ? `#${d.number}` : null;
  const sender = str(d.sender);
  return [
    outcome.length > 0 ? `${head}, ${outcome.join(", ")}` : head,
    number ?? str(d.branch),
    sender && `by ${sender}`,
  ]
    .filter(Boolean)
    .join(" · ");
}

/** The cancelled, closed-pull-request and deleted-branch lines a restart and a redo share. */
function removedLines(d: Record<string, unknown>) {
  const cancelled = runAt(d.cancelled);
  const closed = listOf(d.closed_prs, prNumber);
  const deleted = listOf(d.deleted_branches, str);
  return {
    cancelled: cancelled && `Canceled ${cancelled}`,
    closed: closed && `Closed pull requests ${closed}`,
    deleted: deleted && `Deleted branches ${deleted}`,
  };
}

function prNumber(value: unknown): string | null {
  const number = issueNumber(value);
  return number && `#${number}`;
}

/** What a restart took down (`ticket_restarted`), a line each, or null when it took nothing. */
function restartRemoved(d: Record<string, unknown>): string | null {
  const { cancelled, closed, deleted } = removedLines(d);
  const abandoned = runAt(d.abandoned);
  return (
    [cancelled, abandoned && `Closed the paused session of ${abandoned}`, closed, deleted]
      .filter(Boolean)
      .join("\n") || null
  );
}

/** What a redo did (`step_redone`), a line each, or null when it did nothing beyond the move. */
function redoRemoved(d: Record<string, unknown>): string | null {
  const { cancelled, closed, deleted } = removedLines(d);
  const parked = runAt(d.ended_parked);
  const kept = listOf(d.kept_branches, str);
  const merged = listOf(d.merged_prs_left, prNumber);
  return (
    [
      cancelled,
      parked && `Ended the paused session of ${parked}`,
      closed,
      deleted,
      kept && `Kept branches ${kept}, which an earlier step uses`,
      merged &&
        `Left pull requests ${merged} merged, because a merge can't be undone and a later check can still see it`,
    ]
      .filter(Boolean)
      .join("\n") || null
  );
}

/** `{step, attempt}` as "impl attempt 2", or null for anything else. */
function runAt(value: unknown): string | null {
  if (typeof value !== "object" || value === null) return null;
  const { step, attempt } = value as { step?: unknown; attempt?: unknown };
  const name = str(step);
  return name && typeof attempt === "number" ? `${name} attempt ${attempt}` : null;
}

/** What a step still needs, one per line, then the rules that said so. Rows from before `needs`
 *  existed carry the rules alone. */
function neededBody(needs: string[], rules: string[]): string {
  if (!needs.length) return rules.join("\n");
  if (!rules.length) return needs.join("\n");
  return `${needs.join("\n")}\n\nRules checked:\n${rules.join("\n")}`;
}

/** The string items of a list detail, none for anything else. */
function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((f): f is string => typeof f === "string") : [];
}

/** A list's readable items, comma-joined, or null when it has none. */
function listOf(value: unknown, item: (entry: unknown) => string | null): string | null {
  const items = Array.isArray(value)
    ? value.map(item).filter((entry): entry is string => entry !== null)
    : [];
  return items.length > 0 ? items.join(", ") : null;
}

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}
