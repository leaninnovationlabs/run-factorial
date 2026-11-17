"""The closed set of event types (design.md §5).

The `event.type` column is TEXT with no CHECK, so the log can record a type an older deployment
does not know. This enum is therefore the application-side closed set: writers use it, readers
may see strings that are not in it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Final

__all__ = [
    "REQUESTED_VIA_WORDS",
    "EventType",
    "RequestedVia",
    "ResumedVia",
    "aware_time",
    "requested_via",
]


class EventType(StrEnum):
    """design.md §5's event types, plus later additions, each explained where it is declared."""

    TICKET_ADMITTED = "ticket_admitted"
    STEP_STARTED = "step_started"
    #: `AGENT_SPAWNED` carries `branch_heads` for a step that declares `preview.files`: each
    #: observed branch its `workspace.branch` covers, as a list of `{name, sha}`, as the run
    #: started (tech-stack decision 71); events before that carry a name -> sha map. Its output
    #: record diffs from there.
    AGENT_SPAWNED = "agent_spawned"
    #: `AGENT_COMPLETED` carries `while_parked`, the park's reason, when a parked session's
    #: re-check accepted the run without a resume (`agent_steps.recheck_parked`).
    AGENT_COMPLETED = "agent_completed"
    AGENT_FAILED = "agent_failed"
    POSTCONDITION_FAILED = "postcondition_failed"
    GATE_WAITING = "gate_waiting"
    #: `GATE_APPROVED` and `GATE_REJECTED` carry `qualified_as` (`named`, `permission` or
    #: `open`) and `permission` (the role GitHub reported, for `permission` only): a role can
    #: change later, so the event keeps why the reviewer counted then. Both are null on a
    #: rejection no review decided. A `GATE_REJECTED` that a change request on a step with a page
    #: caused also carries `page_branch` and `page_sha`, where the page stood then.
    GATE_APPROVED = "gate_approved"
    GATE_REJECTED = "gate_rejected"
    STEP_COMPLETED = "step_completed"
    RETRY_SCHEDULED = "retry_scheduled"
    ESCALATED = "escalated"
    TICKET_DONE = "ticket_done"

    #: Beyond design.md §5's list. The ticket moved ONTO a step: appended in the same unit of
    #: work as the `ticket.current_step` assignment, at every site that makes one. It exists
    #: because the ticket page draws its graph from `current_step` and its timeline from this log,
    #: so without this row the graph names a step the log has not mentioned — for however long the
    #: step takes to produce its first run.
    #:
    #: NOT a substitute for `STEP_STARTED`, which still means "a `step_run` row was created". A
    #: step can be entered and never started: unmet preconditions decline silently, and an
    #: infrastructure spawn refusal rolls its own row and event back while this one stands.
    #:
    #: `attempt` is a PREDICTION — the number the next run at this step WILL carry
    #: (`worker/tick_context._next_attempt`), stamped before any row exists so that this event and
    #: that run file under one `step#attempt` on the ticket page. Every other event's `attempt`
    #: names a row that already exists; here it may never be redeemed, and the log keeps the
    #: prediction. Entering the TERMINAL step carries `attempt: null`, matching `TICKET_DONE`, so
    #: the two land in one block rather than splitting the terminal node across two.
    STEP_ENTERED = "step_entered"

    #: Beyond design.md §5's list. Emitted by `worker.reconcile`'s `_UNBLOCK` gesture when the
    #: unblock proceeds: a human removed `factorial:blocked` from a BLOCKED ticket, or asked for
    #: it with `UNBLOCK_REQUESTED`, or the worker's spawn re-check found that a runtime which
    #: refused a session can run one now (`agent_steps.spawn_cleared`). `via` is `label`, a
    #: request's `requested_via`, or `recheck`, and `request_id` names the request it answers. A
    #: refused label removal (no retry policy for the step) emits nothing, because nothing
    #: transitioned; a refused request is answered by `UNBLOCK_REFUSED`.
    #: Not step-scoped, like `TICKET_DONE`.
    #:
    #: Names two attempts, like `RETRY_SCHEDULED`: `attempt` is the blocked run's, and
    #: `next_attempt` is the one this gesture GRANTS — a prediction, redeemed when the following
    #: tick's starter numbers its row (both read `worker/tick_context._next_attempt`). Readers group
    #: by the second: an un-block filed under the exhausted attempt alone leaves the granted one
    #: with nothing on the ticket page until `step_started`, which is a poll interval away at best
    #: and the rest of the failed run's backoff away at worst.
    #:
    #: `due_at` is when that backoff clears, measured from the failed run's `ended_at` because
    #: that is what `_retry_due` reads. Null wherever the wait is not a clock: a gate reopens on
    #: the next tick, and an unmet precondition is released by a human acting elsewhere.
    UNBLOCKED = "unblocked"

    #: A human asked for an escalated ticket to be un-blocked, from the dashboard or through the
    #: MCP server. It is the request form of removing `factorial:blocked`. Ticket-scoped. Carries
    #: `requested_by`, `requested_via`, `mcp_token_id` (MCP only), `step` and `attempt`. Answered
    #: by an `UNBLOCKED` whose `request_id` names it, or by `UNBLOCK_REFUSED`. A label removal's
    #: `UNBLOCKED` answers it too.
    UNBLOCK_REQUESTED = "unblock_requested"

    #: The loop declined an `UNBLOCK_REQUESTED`: `request_id`, `step`, `attempt` and `reason`.
    #: The reason is `parked` (an interrupted session waits on Resume instead), `stale` (the
    #: ticket escalated again after the request) or `no_retry_policy`. A refused label gesture
    #: still writes nothing.
    UNBLOCK_REFUSED = "unblock_refused"

    #: The loop gave up on a ticket whose tick raised `worker.quarantine_after` times in a row.
    #: Carries `error` (the exception's text) and `failures`. Not step-scoped: the tick died
    #: before it knew which step it was on, which is why no `agent_failed` fits.
    TICKET_QUARANTINED = "ticket_quarantined"

    #: A spawn could not reach its substrate's service (`runtime.contract.SubstrateUnavailable`,
    #: an E2B outage): the tick rolled back and is retried, and it does not count toward
    #: quarantine. Written once per streak, on the first such tick. Carries `error`.
    SUBSTRATE_UNAVAILABLE = "substrate_unavailable"

    #: The recovery half of `TICKET_QUARANTINED`: an operator asked for a requeue and the loop
    #: may lease the ticket again. Carries `requested_by`, `requested_via` and the `failures`
    #: count being cleared, which survives nowhere else once the column is zeroed.
    #:
    #: WRITTEN by the API rather than requested from it, alone among the operator gestures. The
    #: rest append a request the loop honours on its next pass, but the loop never leases a
    #: quarantined ticket, so a request here would sit unread for ever — which is why the route
    #: clears the flag itself, and why the record of it is written in the same transaction.
    TICKET_REQUEUED = "ticket_requeued"

    #: Beyond design.md §5's list: `kind: action` (design.md §4) is a step kind upstream never had.
    #: Mirrors `AGENT_COMPLETED` / `AGENT_FAILED` for the third dispatch arm — an action has no
    #: cost/tokens, but the same "record what happened, then STEP_COMPLETED separately" applies.
    ACTION_EXECUTED = "action_executed"
    ACTION_FAILED = "action_failed"

    #: The fourth dispatch arm's failure event: a `kind: check` step whose conditions were still
    #: unmet at its deadline. Not `AGENT_FAILED`/`ACTION_FAILED`, which would misname what ran —
    #: nothing ran. There is no success twin: conditions holding IS the step completing, and
    #: `STEP_COMPLETED` already records that.
    CHECK_FAILED = "check_failed"

    #: A human asked for a run to stop (decision 26). Recorded by the API and OBSERVED by the
    #: loop, never acted on where it is written: the API does not touch the state machine, so a
    #: stop button is a request the worker honours on its next pass — the same shape a "retry this
    #: step" button would take (`api.routes.tickets`).
    INTERRUPT_REQUESTED = "interrupt_requested"

    #: A human asked for a failed run's retry to start now rather than once
    #: its backoff has run. Recorded by the API against that run, and OBSERVED by the loop like
    #: `INTERRUPT_REQUESTED`: a step whose latest run carries one counts its retry as due
    #: (`worker.tick_context._retry_ready`). Carries `step`, `attempt` (the failed run's),
    #: `next_attempt`, `requested_by` and `requested_via`. Answered by the next attempt's
    #: `step_started`.
    RETRY_REQUESTED = "retry_requested"

    #: A person pressed Retry on a failed step in the dashboard. The next attempt starts now and
    #: continues the failed attempt's session where it can (decision 77). Carries `step`,
    #: `attempt` (the failed run's), `next_attempt`, `requested_by` and `requested_via`.
    #: Answered by the next `step_started` or by `STEP_RETRY_REFUSED`. A redo, a restart or an
    #: exit answers it too.
    STEP_RETRY_REQUESTED = "step_retry_requested"

    #: The loop did not start the attempt a `STEP_RETRY_REQUESTED` asked for. Carries `step`,
    #: `attempt`, `request_id` and `reason` (`worker.retries.RetryRefusal`): `stale` (the run is
    #: no longer its step's latest failed attempt), `gate`, `not_escalated` (the ticket is
    #: blocked on something else), `off_the_floor` (the ticket is neither running nor blocked),
    #: `preconditions_unmet` (with `unmet`, the false leaves), `spawn_refused` (the runtime
    #: refused the spawn, and the ticket is blocked on that), `spawn_failed` (the substrate
    #: failed the spawn, `detail` says how; the ticket stays as it was), `unavailable` (the
    #: forge could not be read for `COMPLETION_UNREADABLE_FOR`),
    #: `sandbox_limit` (the Factorial-managed key's limit is reached, with `scope`, `limit` and
    #: `running` as `SANDBOX_LIMIT_WAITING` has them; the ticket stays as it was) or
    #: `conflict_fixed` (the run was a conflict fix whose pull request needs none now: merged,
    #: closed, or fixed by a person's push; the ticket went back to its gate).
    STEP_RETRY_REFUSED = "step_retry_refused"

    #: The loop honoured that request: the substrate stopped, the workspace was kept, and the
    #: ticket parked. Paired with the request rather than replacing it, because the log has to
    #: answer "was this asked for and not yet done?" — one event alone cannot.
    AGENT_INTERRUPTED = "agent_interrupted"

    #: The run reached a terminal state before the tick that would have stopped it, or, with
    #: `reason: parked`, a park did and a resume is about to start it again. The other answer to a
    #: request: without it an overtaken stop is indistinguishable from an ignored one.
    INTERRUPT_OVERTAKEN = "interrupt_overtaken"

    #: A parked, interrupted run continued in the workspace the interrupt kept. Records who
    #: steered it and how many comments were fed, by reference: the bodies are already on the
    #: ticket, and copying untrusted text — or a credential someone pasted — into the event store
    #: is what `events.writer._reject_credentials` exists to prevent. `via` (`ResumedVia`) says
    #: what released it; an event without one is a label release. It carries `terminal` too, as
    #: `AGENT_SPAWNED` does: where the gateway now attaches.
    AGENT_RESUMED = "agent_resumed"

    #: A session went IDLE with its postconditions unmet (decision 56). Written once
    #: per idle spell, not once per poll. Carries `reason` (the contract's `IdleReason`),
    #: `idle_since`, and `question` when the agent asked one — agent-written text, so data, never
    #: executed. A question or a failed turn makes the spell park for a person at the idle
    #: deadline, for the rest of the active stretch.
    AGENT_IDLE = "agent_idle"

    #: The IDLE session took a turn again. Carries `active_at`, from which the step deadline is
    #: measured afresh: it counts active time, and is not checked while IDLE.
    AGENT_ACTIVE = "agent_active"

    #: The loop is about to call `finish` on an IDLE session: its postconditions held, or, with
    #: `silent: true` and the leaf reasons in `unmet`, it stopped with nothing asked and nobody
    #: engaged, and the ended run fails on them. Carries `idle_since`, naming the spell, and
    #: `sent_at`. Written before the call, so the loop sends it once per spell however long the
    #: run takes to end — once more, with `retry: true`, if the session is still IDLE
    #: `terminal.idle_park_after` later.
    AGENT_FINISHING = "agent_finishing"

    #: The loop parked a session: `reason` is `idle` (it waited past
    #: `terminal.idle_park_after`), `rollover` (its token neared the hour or the substrate's
    #: session cap, and it resumes on the same tick with a fresh one; `mint_age_seconds` says
    #: how old the token was), `ceiling` (it lived `terminal.session_ceiling`; `live_seconds`
    #: says how long) or `exited` (a person ended it from its terminal before it was accepted;
    #: `unmet` says why it was not). `at_prompt`: the session sat at its prompt; always true for
    #: `idle`. A human's stop stays `AGENT_INTERRUPTED`, which the pending-request count pairs
    #: with `INTERRUPT_REQUESTED`.
    AGENT_PARKED = "agent_parked"
    #: The runtime cannot run a session now (`spawn_refusal`), asked before the run's row exists:
    #: the ticket blocks with no attempt spent, and the worker asks again every
    #: `terminal.parked_recheck` and releases it once the refusal clears. Carries `step`,
    #: `runtime` (the slug) and `refusal`, which names the fix.
    SPAWN_REFUSED = "spawn_refused"
    #: A start on the Factorial-managed key waits: its organization, or the whole pool, already
    #: has as many active runs as its limit allows (`worker.sandbox_limit`). No row, no attempt,
    #: no label; the next tick asks again. Carries `step`, `attempt` (the one that would have
    #: started), `scope` (`organization` or `pool`), `limit`, `running` and `runtime` (the slug).
    #: Written once per entry of the step, and again when the held attempt, the scope or the
    #: limit changes.
    SANDBOX_LIMIT_WAITING = "sandbox_limit_waiting"

    #: A human pressed Resume on a parked run (decision 26). Recorded by the API and
    #: OBSERVED by the loop, like `INTERRUPT_REQUESTED`. Carries `by`, `requested_via`,
    #: `step`, `attempt`, `has_answer`, and `answer` when one was given: the text the resumed turn
    #: receives, kept here as `STEP_CHANGES_REQUESTED` keeps `feedback`. Answered by an
    #: `AGENT_RESUMED` whose `request_id` names it, or by `SESSION_RESUME_REFUSED`.
    SESSION_RESUME_REQUESTED = "session_resume_requested"

    #: The loop could not honour a `SESSION_RESUME_REQUESTED`: `request_id` names it and
    #: `reason` says why. Without it a refused request would stay pending for ever.
    SESSION_RESUME_REFUSED = "session_resume_refused"

    #: A rework started a fresh session rather than continuing the previous attempt's. Carries
    #: `previous_step_run_id` and `reason`: `runtime_changed`, `cli_version_changed`,
    #: `past_retention`, `no_session`, `not_kept` (the substrate refused with
    #: `SessionUnavailable`) or `no_branch` (the failed run held no branch, `NoBranchHeld`). An
    #: event older than every run being a session may say `not_interactive`.
    SESSION_RESUME_SKIPPED = "session_resume_skipped"

    #: The terminal gateway (decision 57) attached a viewer, and the only thing it writes besides
    #: the next three. Carries `step`, `attempt`, `by`, `viewer`, `writable` (false since the
    #: writer seat: every viewer attaches read-only), `target` (`session` or `inspect`) and `via`
    #: (`cookie` or `ticket`, the credential's kind).
    TERMINAL_ATTACHED = "terminal_attached"

    #: That viewer left: `step`, `attempt`, `by`, `viewer`, `seconds` and the close `code`.
    TERMINAL_DETACHED = "terminal_detached"

    #: One burst of a writer's typing: `step`, `attempt`, `by`, `viewer` and `bytes`, a count.
    #: Never the content: a terminal is where a human pastes a credential.
    TERMINAL_INPUT = "terminal_input"

    #: A viewer took the run's writer seat: `step`, `attempt`, `by`, `viewer`, `from` (who held
    #: it, null when it was free) and `forced` (taken from that holder). A freed seat writes
    #: nothing.
    TERMINAL_SEAT_TAKEN = "terminal_seat_taken"

    #: A signed-in user asked to open the session of a kept run's workspace. Recorded by the API
    #: and OBSERVED by the loop, like `PREVIEW_REQUESTED`, and its id is the token `inspect_start`
    #: is called with. Carries `step`, `attempt` and `requested_by`. Answered by `INSPECT_STARTED`
    #: or `INSPECT_PARKED`.
    INSPECT_REQUESTED = "inspect_requested"

    #: The session view is open: `step`, `attempt`, `kind` (`session`: the run's own agent TUI,
    #: read-only), `requested_by`, `request_id`, `terminal`, where the gateway attaches, and
    #: `started_at`, the worker's clock its limit is measured from too.
    INSPECT_STARTED = "inspect_started"

    #: The session view closed, or never opened: `step`, `attempt`, `reason`, and `request_id`
    #: when it answers a request. `failed: true` marks a refusal or a start that failed.
    INSPECT_PARKED = "inspect_parked"

    #: A signed-in user closed an open or starting session view at once. Recorded by the API and
    #: OBSERVED by the inspect pass, like `PREVIEW_STOP_REQUESTED`; carries `step`, `attempt` and
    #: `requested_by`. Not part of the inspect lifecycle, whose status stays that of its latest
    #: `INSPECT_*` event. Answered by `INSPECT_PARKED`.
    INSPECT_CLOSE_REQUESTED = "inspect_close_requested"

    #: A human asked for a ticket to leave the factory (decision 29); `requested_by` and
    #: `requested_via` say who, and whether on the dashboard or through the MCP server.
    #: Recorded by the API and OBSERVED by the loop, exactly like `INTERRUPT_REQUESTED`.
    #: Ticket-scoped — no `step_run_id` — because an exit is about the ticket, not a run, but it
    #: still names `step` and `attempt`: the ticket page keys a timeline block on that pair.
    EXIT_REQUESTED = "exit_requested"

    #: The loop took the ticket off the floor: live work cancelled, status `exited`,
    #: `factorial:exit` projected, `closed_at` set. `via` names which way in fired — `dashboard`,
    #: `mcp`, `label`, `issue_closed`. `attempt` is the LATEST run at the step (null when none
    #: ran), so the row files under the block the reader is looking at; `cancelled` says whether
    #: that run was the one this exit stopped, which a blocked ticket's exhausted run was not.
    #: Answers `EXIT_REQUESTED` for the pending count; for the other two causes it stands alone.
    TICKET_EXITED = "ticket_exited"

    #: A human asked for an exited ticket back. Same shape and same honesty
    #: as `EXIT_REQUESTED`, `step`/`attempt` included.
    REENTRY_REQUESTED = "reentry_requested"

    #: The loop brought the ticket back. The same release `UNBLOCKED` records, so the same
    #: fields — `attempt`, `next_attempt`, `due_at` — plus `via` (`dashboard` | `mcp` | `label`) and
    #: `resumed` (true when an interrupted session continued rather than a fresh attempt
    #: being granted, in which case `next_attempt == attempt`).
    TICKET_REENTERED = "ticket_reentered"

    #: The loop declined a re-entry: `reason` is `issue_closed` (reopen first) or
    #: `no_retry_policy` (the pinned process cannot grant an attempt), under the same
    #: `step`/`attempt` the request carried. Answers `REENTRY_REQUESTED`; for a label gesture the
    #: re-asserted label is the answer.
    REENTRY_REFUSED = "reentry_refused"

    #: A human asked for a ticket to restart on its lane's current snapshot (decision 67). Carries
    #: `target_process_version_id` and `target_sha` — the snapshot the asker saw, which the loop
    #: applies — `allow_current_pin`, `step`/`attempt` like `EXIT_REQUESTED`, and the actor fields.
    #: Ticket-scoped, recorded by the API and OBSERVED by the loop.
    RESTART_REQUESTED = "restart_requested"

    #: The loop restarted the ticket: live work cancelled, previews stopped, its pull requests
    #: closed and branches deleted, `generation` bumped, the pin moved, and the entry step entered.
    #: Carries `request_id`, the request's `requested_by`/`requested_via`, `process` (the lane),
    #: `from_process_version_id`/`from_sha`, `to_process_version_id`/`to_sha`, `generation`,
    #: `entry_step` (also as `step`, with a null `attempt`, written before its `step_entered`),
    #: `cancelled` and `abandoned` (`{step, attempt}` of the run stopped and of the interrupted
    #: session left to the reaper, or null), `closed_prs`, `deleted_branches` and `owner`. Answers
    #: `RESTART_REQUESTED`, and every re-entry, un-block and resume request asked before it.
    TICKET_RESTARTED = "ticket_restarted"

    #: The loop declined a restart: `reason` is `issue_closed`, `lane_retired`, `merged_outside`,
    #: `cancel_failed` or `exited` (an exit won the same tick), with `detail` for a reader, and
    #: the `request_id`, `step` and `attempt`. Answers `RESTART_REQUESTED`.
    RESTART_REFUSED = "restart_refused"

    #: A person asked, from the dashboard, for a step and every step after it to start again
    #: from a clean start (decision 76). Carries `target_step`, `has_note`, the optional `note`
    #: (the one place its text is kept), `step`/`attempt`/`generation` as the asker saw them, and
    #: the actor fields. Ticket-scoped, recorded by the API and OBSERVED by the loop.
    REDO_REQUESTED = "redo_requested"

    #: The loop redid the step: the runs of `discarded_steps` ended and set aside, their pull
    #: requests closed and branches deleted, and `target_step` entered (also as `step`, with a
    #: null `attempt`, written before its `step_entered`). Carries `request_id`, the actor fields,
    #: `from_step`, `generation`, `superseded_runs` (`{step, attempt}` each), `cancelled` and
    #: `ended_parked` (the live run cancelled and the parked session ended, or null),
    #: `closed_prs`, `deleted_branches`, `kept_branches` (a kept step's too), `merged_prs_left`
    #: (nothing un-merges) and `has_note`. Answers `REDO_REQUESTED`, and every Resume, un-block,
    #: step retry and completion request asked before it.
    STEP_REDONE = "step_redone"

    #: The loop declined a redo: `reason` is `issue_closed`, `exited`, `stale` or `cancel_failed`
    #: (`worker.redos.RedoRefusal`), with `detail` for a reader, and the `request_id`,
    #: `target_step`, `step` and `attempt`. Answers `REDO_REQUESTED`.
    REDO_REFUSED = "redo_refused"

    #: A producer published, replaced or withdrew the URLs a step run's output can be viewed at
    #: (`events.previews`). Carries `urls`, a list of `{service, url}` (empty when withdrawn;
    #: `service` is null for a producer's URL), and `published_via`. Written by whoever
    #: publishes, not requested from the loop: it changes no state the loop owns, and the latest
    #: one per run IS the run's current preview. Unchanged URLs are not written again.
    PREVIEW_URL_UPDATED = "preview_url_updated"

    #: A signed-in user pressed Start on a run whose sandbox is kept for its preview (decision
    #: 53). Recorded by the API and OBSERVED by the loop, like `INTERRUPT_REQUESTED`. Carries
    #: `requested_by`, and `restart` when a person restarts a serving preview, or `renewal` when
    #: the loop starts it again after its sandbox renewed, `requested_by` then `factorial`. Its id
    #: is the token `preview_start` is called with, so no request starts the preview twice.
    #: Answered by `PREVIEW_STARTED`, `PREVIEW_FAILED` or `PREVIEW_STOPPED`.
    PREVIEW_REQUESTED = "preview_requested"

    #: A signed-in user pressed Stop on an awake preview. Carries `requested_by`. Answered by
    #: `PREVIEW_STOPPED`.
    PREVIEW_STOP_REQUESTED = "preview_stop_requested"

    #: Every service answered its ready check. Carries `services`, `head_sha` and `uncommitted`
    #: — what the sandbox reported, so advisory. The URLs are `PREVIEW_URL_UPDATED`'s, written
    #: beside it: one record per fact.
    PREVIEW_STARTED = "preview_started"

    #: Setup failed, a service never answered, or one stopped answering; or the loop could not
    #: start it. Carries `reason`, which may quote output from the sandbox: data, never executed.
    PREVIEW_FAILED = "preview_failed"

    #: The preview no longer serves: someone stopped it, its sandbox was released, or the
    #: runtime closed it. Carries `reason`: `idle` when nobody kept it for `stopAfter`, `renewal`
    #: when its sandbox paused at its session cap (the loop may start it again), `continued` when
    #: a rework carried it to its run. A renewal whose park failed reads `renewal` then the
    #: cause on the next line, and is not one the loop starts again from.
    PREVIEW_STOPPED = "preview_stopped"

    #: A signed-in user pressed Keep running on an awake preview. Carries `requested_by`. Not a
    #: lifecycle event: it only moves when the preview closes for idle (`events.previews`).
    PREVIEW_KEPT = "preview_kept"

    #: No longer appended: the log keeps those written while `preview.page` published a summary to
    #: a branch of its own, which a declared file replaced (decision 71), and the activity still
    #: words them. Where a run's page landed, as the worker read it on GitHub when the run ended,
    #: once per run. Carries `status` and what that status needs: `branch`, `path`, `commit`,
    #: `blob`, `size` and an optional `unchanged_since` when `published`; `branch` and `path` globs
    #: when `missing`; `matches` when `ambiguous`; `since_attempt` when `not_republished`; `reason`
    #: when `lookup_failed`.
    PREVIEW_PAGE_OBSERVED = "preview_page_observed"

    #: What a run of a step that declares `preview.files` changed on its branch, as the worker
    #: read it on GitHub (tech-stack decision 71): once when the run ended, and on a step that
    #: awaits review also once per idle spell, so the newest record is the work as of the
    #: session's latest stop. The record is the step's footprint, wider than what it declares; the
    #: API selects from it. Carries `status` and what that status needs: `branch`, `commit` (the
    #: branch head at that stop), `start` (where the diff began), `start_at_spawn`, `slug` (the
    #: run's, else that of the one branch its glob found, or null), `cut_off` and `files` (each
    #: `path`, `blob`, `size` and `changed`, null where unknown) when `observed`; `branch` as a
    #: glob when `no_branch`; `matches` when `ambiguous`; `reason` when `lookup_failed`.
    RUN_OUTPUTS_OBSERVED = "run_outputs_observed"

    #: A human asked, from the dashboard, for changes to a step whose preview they reviewed.
    #: Recorded by the API against the waiting gate's run and OBSERVED by the loop, like
    #: `INTERRUPT_REQUESTED`. Carries `feedback` (the text the rework receives verbatim),
    #: `reviewed_step_run_id`, `requested_by` (a Google email, or `api-token` for the shared
    #: bearer token — decision 12) and `idempotency_key`. Answered by a `GATE_REJECTED` whose
    #: `change_request_id` names it, or by `STEP_CHANGES_REFUSED`.
    STEP_CHANGES_REQUESTED = "step_changes_requested"

    #: The loop could not honour a `STEP_CHANGES_REQUESTED`: `request_id` names it and `reason`
    #: says why — the pull request merged first, none was open, or a newer attempt exists.
    STEP_CHANGES_REFUSED = "step_changes_refused"

    #: A person pressed Force complete in the dashboard: check the run's postconditions now, and
    #: accept it when they hold (decision 78). Recorded by the API against the run, with `step`,
    #: `attempt`, `allow_unmet`, `seen_failures` (the failures the person read, when overriding),
    #: `requested_by` and `requested_via`. Answered once, by `STEP_ACCEPTED_ON_REQUEST`,
    #: `STEP_FORCED` or `COMPLETION_REFUSED`, each naming it by `request_id`.
    COMPLETION_REQUESTED = "completion_requested"

    #: A person approved, from the dashboard, the run of a step whose postconditions call
    #: `request_approval()`, at the commit its branch ended on then. Recorded by the API against
    #: the run, with `step`, `attempt`, `head_sha`, `branch`, `approved_by` and
    #: `requested_via`. It queues nothing: the predicate reads the newest one at acceptance
    #: (`events.approvals`), and a push after it leaves it approving an older commit.
    STEP_APPROVED = "step_approved"

    #: The loop did not complete the run a `COMPLETION_REQUESTED` named. Carries `step`,
    #: `attempt`, `request_id`, `reason` (`worker.completions.CompletionRefusal`), `detail`,
    #: `failures` (every false leaf, for `unmet`) and `overridable`.
    COMPLETION_REFUSED = "completion_refused"

    #: The answer to a `COMPLETION_REQUESTED` whose postconditions held: the run is SUCCEEDED and
    #: the ticket moves on. Carries `step`, `attempt`, `request_id`, `requested_by`,
    #: `requested_via` and `from_status` (the run's status before). An INTERRUPTED or IDLE run
    #: also gets an `AGENT_COMPLETED` for the result read from it, carrying
    #: `accepted_on_request` (who asked).
    STEP_ACCEPTED_ON_REQUEST = "step_accepted_on_request"

    #: The answer to a `COMPLETION_REQUESTED` with `allow_unmet` whose conditions still failed,
    #: every one among those the person read: the run is SUCCEEDED with `forced_by` set and the
    #: ticket moves on. Carries `step`, `attempt`, `request_id`, `requested_by`,
    #: `requested_via`, `failures` (every false leaf) and `from_status`. An INTERRUPTED or IDLE
    #: run's `AGENT_COMPLETED` carries `forced_by` instead of `accepted_on_request`.
    STEP_FORCED = "step_forced"

    #: The step after a forced one is waiting: an agent step's preconditions are unmet, or a
    #: gate finds no pull request. Carries `step`, `attempt`, `from_step`, `forced_by` and
    #: `unmet` (the false leaves, or the gate's missing match). Written once per entry of the
    #: step, and only when the step it came from was forced.
    NEXT_STEP_WAITING = "next_step_waiting"

    #: A gate's approval stood, and GitHub refused the merge `onApprove` asked for — branch
    #: protection, a conflict, or a head that moved. The gate stays waiting and the merge is tried
    #: again every tick. Carries `pr_number`, `head_sha`, `approved_by`, `status` and `reason`
    #: (GitHub's message). Written once per distinct refusal on the gate's run, not once per tick.
    GATE_MERGE_REFUSED = "gate_merge_refused"

    #: A review a waiting gate weighed and set aside (`worker/reviewers.py`). Carries
    #: `pr_number`, `reviewer`, `review_id`, `verdict` (`approved` or `changes_requested`),
    #: `reason` (`app`, `bot`, `not_named` or `below_permission`), `permission` (the role seen,
    #: for `below_permission`) and `required` (the gate's `approverPermission`). Written once per
    #: review id per ticket, and never in a poll where the gate decides.
    GATE_REVIEW_IGNORED = "gate_review_ignored"

    #: A waiting gate's pull request closed without a merge, so the gate holds instead of
    #: rejecting (decision 79). Carries `pr_number` and `head_sha`. Written once per pull request
    #: per gate run, with one comment on the ticket.
    GATE_PR_CLOSED = "gate_pr_closed"

    #: A merging gate's reading of its pull request's mergeability. Carries `step`, `attempt`,
    #: `pr_number`, `head_sha`, `base_ref` and `state` (`conflicting`, `behind` or `clean`).
    GATE_MERGEABILITY = "gate_mergeability"

    #: A person asked, from the dashboard, for a conflicting pull request to be fixed. Carries
    #: `requested_by` and `idempotency_key`. Answered by `GATE_CONFLICT_SENT_BACK` or
    #: `CONFLICT_FIX_REFUSED`.
    CONFLICT_FIX_REQUESTED = "conflict_fix_requested"

    #: The loop could not honour a `CONFLICT_FIX_REQUESTED`: `request_id` names it and `reason`
    #: says why (`no_fix_step`, `no_pull_request`, `already_merged`, `mergeable`,
    #: `newer_attempt` or `unsafe_branch`).
    CONFLICT_FIX_REFUSED = "conflict_fix_refused"

    #: A merging gate sent its conflicting pull request back to an agent step. Carries
    #: `pr_number`, `head_ref`, `base_ref`, `head_sha`, `state`, `target_step`, `request_id` (null
    #: when the loop started it) and `requested_by`. It is never a `GATE_REJECTED`.
    GATE_CONFLICT_SENT_BACK = "gate_conflict_sent_back"

    #: An open issue the admission pass declined for a reason an operator has to fix: no default
    #: process configured, an unknown process label, or two of them (`worker.selection`). Carries
    #: `external_key`, `reason` and `detail`, and no `ticket_id` — there is no ticket, which is
    #: the finding.
    #:
    #: Written at most once per `(project, issue, reason)`: a refusal that stays true is still
    #: true on the next tick, and one row per tick would be the log's largest family. The
    #: *current* answer is `GET /projects/{id}/pending`, which recomputes it; an append-only log
    #: can say what happened but never whether it is still happening.
    ADMISSION_REFUSED = "admission_refused"

    #: Project-scoped: a caller asked the orchestrator to open an issue as the App. Carries
    #: `requested_by`, `requested_via`, `mcp_token_id`, `idempotency_key`, `args_digest`, `repo`,
    #: `process`, `labels` and `assignee`. It never carries the title or the body, because
    #: `ticket.title` stays the only issue text kept. Recorded before the GitHub call, so a lost
    #: answer is recovered by its marker and the issue is not opened again.
    ISSUE_CREATE_REQUESTED = "issue_create_requested"

    #: GitHub opened that issue: `request_id`, `repo`, `number`, `labels`, `assignees` and
    #: `recovered`. `recovered` is true when the issue was found by its marker after an
    #: unanswered request instead of being created now.
    ISSUE_CREATED = "issue_created"

    #: Beyond design.md §5's list, and not a state transition: a webhook is only a wake-up signal
    #: (design.md §2.1). Logging it makes "did the delivery arrive?" answerable when a poll and a
    #: webhook disagree, without making the webhook authoritative. Details: `repo`,
    #: `github_event`, `delivery`, `woke` (`ticket`, `admission` or `project`), and what GitHub
    #: says the delivery was about where it says it — `action`, `sender`, `number`,
    #: `on_pull_request`, `branch`, `merged`, `label`, `review_state`, `conclusion`. Never a
    #: title, body or comment.
    WEBHOOK_RECEIVED = "webhook_received"

    #: Project-scoped, details `{branch, sha}`: the orchestrator seeded the config branch
    #: (issue #248). The sync guard reads these as the commits the orchestrator itself wrote.
    CONFIG_BRANCH_CREATED = "config_branch_created"

    #: Project-scoped, details `{branch, sha, process}`: the builder committed to the config
    #: branch on an operator's click. Recorded before the ref moves; the sync guard accepts these.
    CONFIG_COMMITTED = "config_committed"

    #: Project-scoped, details `{branch, sha, known_base, commits, requested_by, requested_via}`:
    #: an operator reviewed a head no event named and no human merged (a direct push), and
    #: vouched for it from the dashboard. The sync guard accepts `sha`, and so every commit
    #: between `known_base` and it; never written over MCP.
    CONFIG_HEAD_ADOPTED = "config_head_adopted"


class ResumedVia(StrEnum):
    """What released a parked session: `AGENT_RESUMED.via`."""

    LABEL = "label"
    DASHBOARD = "dashboard"
    MCP = "mcp"
    #: The loop's own park and resume for a fresh token (decision 56). It continues the active
    #: stretch it interrupted, so it never re-bases the step deadline.
    ROLLOVER = "rollover"


class RequestedVia(StrEnum):
    """The surface a human's request came through: `requested_via` on a request event."""

    DASHBOARD = "dashboard"
    MCP = "mcp"
    #: A proposal of the assistant's, run by its conversation's owner from the dashboard.
    CHAT = "chat"


#: How the worker's messages name each surface (`worker/exits.py`, `worker/interrupts.py`).
REQUESTED_VIA_WORDS: Final[dict[RequestedVia, str]] = {
    RequestedVia.DASHBOARD: "from the dashboard",
    RequestedVia.MCP: "through the Factorial MCP server",
    RequestedVia.CHAT: "from the assistant",
}


def requested_via(details: Mapping[str, object] | None) -> RequestedVia:
    """The surface a request event names.

    Rows written before the MCP server existed carry `dashboard` or nothing. A value this
    deployment does not know reads as the dashboard, so the tick that reads it does not fail.
    """
    value = details.get("requested_via") if details else None
    if not isinstance(value, str):
        return RequestedVia.DASHBOARD
    try:
        return RequestedVia(value)
    except ValueError:
        return RequestedVia.DASHBOARD


def aware_time(value: object) -> datetime | None:
    """An ISO time read from data others wrote, None unless it parses with a zone: a naive one
    would raise against an aware clock."""
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return None
    return at if at.tzinfo is not None else None
