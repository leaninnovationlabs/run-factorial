import { type ComponentProps, useCallback, useMemo, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { isNotFound } from "../api/client";
import {
  useInterruptRun,
  useProject,
  useRefreshTicket,
  useTicket,
  useTicketGatePullRequest,
  useTicketProcess,
  useTimeline,
} from "../api/queries";
import type { StepRun } from "../api/types";
import {
  type ActivityOrder,
  ActivityPanel,
  type ActivitySegment,
  attemptKeyForNode,
  declaresFiles,
  type ForceOffer,
  forceOffer,
  gatePullNumber,
  gateReview,
  hasPreview,
  heldAt,
  type LongContent,
  type NamedSection,
  nextNeeds,
  OutputDrawer,
  retryOffer,
  reviewTarget,
  type Spawn,
  segmentsFrom,
  sortSegments,
  spawnsFrom,
  stepAwaitsReview,
  totalsFrom,
  useReviewOffer,
} from "../components/activity";
import {
  attemptBadges,
  conditionCalls,
  isProcessConfigLike,
  nodeTones,
  ProcessFlowGraph,
  visitedNodeIds,
} from "../components/flow";
import { refreshNote } from "../components/tickets/refreshNote";
import { requestNote } from "../components/tickets/requestNote";
import { TicketActions } from "../components/tickets/TicketActions";
import { TicketFacts, TicketTotals } from "../components/tickets/TicketFacts";
import { TicketMessages } from "../components/tickets/TicketMessages";
import {
  type ConfirmPopupHandle,
  EmptyState,
  ErrorMessage,
  IconButton,
  LinkButton,
  PageHeader,
  Panel,
  Spinner,
  staleSince,
  TextDrawer,
  Toggle,
  useActionFeedback,
  usePageTitle,
} from "../components/ui";
import { forgeHost } from "../forge";
import { conditionOf, latestRunAt, latestRunStatusAt } from "../status";
import { usePersistentState } from "../storage";

/** The activity's Focused switch, remembered by this browser. On until someone switches it off:
 *  the audit trail is the first thing a reader wants, and the routine rows are one press away. */
export const FOCUSED_STORAGE_KEY = "sf-ui:activity-focused";
const parseFocused = (stored: string | null) => stored !== "0";
const serializeFocused = (on: boolean) => (on ? "1" : "0");

export function TicketPage() {
  const ticketId = Number(useParams().ticketId);
  const ticketQuery = useTicket(ticketId);
  const { data: ticket, isPending, error } = ticketQuery;
  const timeline = useTimeline(ticketId);
  const snapshot = useTicketProcess(ticketId, ticket?.process_version_id);
  // Enabled only once the ticket has told us which project it belongs to.
  const project = useProject(ticket?.project_id ?? 0);

  // The block the reader is in. It scopes nothing — everything is on screen at once — but it is
  // what the graph's selected node is bound to, in both directions.
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [text, setText] = useState<{ content: LongContent; subtitle: string } | null>(null);
  // The Output drawer is bound to one run, named in the URL so it can be linked to: `?terminal=`
  // and `?preview=` open that section alone, and `?output=` opens both — the idle park's comment
  // links `?terminal=` (decision 26).
  const [searchParams, setSearchParams] = useSearchParams();
  const named = NAMED_SECTIONS.find((section) => searchParams.has(section));
  const outputRunId = Number(searchParams.get(named ?? "output")) || null;
  /** The run a change request sent back. Sending closes the drawer, and once the log holds the
   *  request the activity goes to its latest event, so the reader sees the request land. */
  const [sentBack, setSentBack] = useState<number | null>(null);
  // Newest first: someone opens this page because a ticket is stuck or a run is live, and the
  // event that answers either question is the last one written.
  const [order, setOrder] = useState<ActivityOrder>("newest");
  const [focused, setFocused] = usePersistentState(
    FOCUSED_STORAGE_KEY,
    parseFocused,
    serializeFocused,
  );
  /** Non-zero while the drawer was opened by Request changes: its form opens at once. */
  const [changesOpen, setChangesOpen] = useState(0);
  /** The Force complete whose dialog is open, kept after the ticket moves on (`ForceCompleteProps`). */
  const [forceHeld, setForceHeld] = useState<ForceOffer | null>(null);

  const entries = useMemo(() => timeline.data?.entries ?? [], [timeline.data]);
  const runs = useMemo(() => ticket?.step_runs ?? [], [ticket]);
  // Where the ticket stands is read from its current generation alone, without the runs a redo
  // set aside. A restart or a redo leaves the earlier runs in the log and in the totals, and out
  // of the graph, the status and the offers to act: attempt numbers keep counting across both,
  // so only the generation and `superseded_at` tell them apart.
  const generation = ticket?.generation;
  const currentRuns = useMemo(
    () => runs.filter((run) => run.generation === generation && !run.superseded_at),
    [runs, generation],
  );
  const logOrder = useMemo(() => segmentsFrom(entries, runs), [entries, runs]);
  const segments = useMemo(() => sortSegments(logOrder, order), [logOrder, order]);
  const totals = useMemo(() => totalsFrom(runs), [runs]);
  // What the graph blinks and what the activity region blinks are the same decision, made once.
  const held = useMemo(() => (ticket ? heldAt(ticket, currentRuns) : null), [ticket, currentRuns]);
  // The one status this page states, in the header and over the graph both. `blocked` alone
  // would call an interrupted run a failure — see `status.ts`.
  const condition = useMemo(
    () =>
      ticket
        ? conditionOf({
            status: ticket.status,
            latestRunStatus: latestRunStatusAt(currentRuns, ticket.current_step),
            parkReason: ticket.park_reason,
            quarantined: ticket.quarantined_at !== null,
          })
        : null,
    [ticket, currentRuns],
  );
  const gatePullQuery = useTicketGatePullRequest(
    ticketId,
    ticket != null && condition === "waiting_gate",
  );
  const tones = useMemo(() => nodeTones(currentRuns, ticket ?? null), [currentRuns, ticket]);
  const spawns = useMemo(() => spawnsFrom(entries), [entries]);
  const interrupt = useInterruptRun(ticketId);
  useActionFeedback(interrupt, { errorTitle: "Couldn't interrupt the run" });
  const refresh = useRefreshTicket(ticketId);
  const refreshFeedback = useActionFeedback(refresh, {
    errorTitle: "Couldn't refresh the ticket",
  });
  // The quarantine message's remedy arms the header's requeue rather than being a second one.
  const requeueControl = useRef<ConfirmPopupHandle>(null);
  // The gate message's Redo arms the header's, as with requeue.
  const redoControl = useRef<ConfirmPopupHandle>(null);
  const closeAndExitControl = useRef<ConfirmPopupHandle>(null);
  usePageTitle([ticket ? `#${ticket.external_key}` : null, project.data?.name]);

  // The snapshot is `dict[str, Any]` on the wire, so a partial one draws no graph rather than
  // throwing on a pointer the mapper expects.
  const pinnedConfig = useMemo(() => {
    const pinned = snapshot.data?.content;
    return isProcessConfigLike(pinned) ? pinned : null;
  }, [snapshot.data]);

  // The latest attempt at the ticket's step: the only parked run whose terminal holds Resume.
  const latestRun = ticket ? latestRunAt(currentRuns, ticket.current_step) : null;
  // This page owns the review's poll; the drawer's Approve reads the same cache. A stop where
  // this run pushed nothing, such as a question on a branch an earlier step made, is not yet a
  // review, nor is one Approve would refuse.
  const latestReview = useReviewOffer(
    latestRun,
    latestRun !== null && stepAwaitsReview(pinnedConfig, latestRun.step_name),
    { parkReason: ticket?.park_reason ?? null, poll: true },
  );

  const openOutput = useCallback(
    (stepRunId: number, section?: NamedSection) => {
      setSearchParams((current) => {
        const next = withoutOutput(current);
        next.set(section ?? "output", String(stepRunId));
        return next;
      });
    },
    [setSearchParams],
  );
  const closeOutput = useCallback(() => {
    setChangesOpen(0);
    setSearchParams(withoutOutput);
  }, [setSearchParams]);
  // A run has a terminal once it spawned, and a preview when its step's pinned config declares one.
  const outputOpener = useCallback(
    (segment: ActivitySegment) => {
      const terminal = segment.run?.runtime_run_id
        ? (run: StepRun) => openOutput(run.id, "terminal")
        : undefined;
      const preview = hasPreview(pinnedConfig, segment.step)
        ? {
            label: declaresFiles(pinnedConfig, segment.step)
              ? ("Files" as const)
              : ("Preview" as const),
            open: (run: StepRun) => openOutput(run.id, "preview"),
          }
        : undefined;
      return terminal || preview ? { terminal, preview } : undefined;
    },
    [pinnedConfig, openOutput],
  );

  const runtimeFor = useCallback(
    (run: StepRun) => (run.runtime_run_id ? spawns.get(run.runtime_run_id) : undefined),
    [spawns],
  );

  /**
   * The one block that has been ENTERED but has not started: the step the ticket is on now, with
   * no `step_run` behind it. That block is the only silent one on the page whose silence has an
   * explanation the page can give.
   *
   * Keyed on `step#attempt`, like `heldAt`, not on the step name. A block with a run has already
   * started, so its preconditions held; and two run-less blocks of one step can be on screen at
   * once while the ticket and timeline polls disagree, where only the newest attempt is actually
   * waiting and the earlier one is history.
   */
  const waitingKey = useMemo(() => {
    if (!ticket) return null;
    const waiting = logOrder
      .filter(
        (s) =>
          s.generation === ticket.generation &&
          s.nodeId === ticket.current_step &&
          s.run === null &&
          s.attempt !== null,
      )
      .reduce<ActivitySegment | null>(
        (best, s) => (best === null || (s.attempt ?? 0) > (best.attempt ?? 0) ? s : best),
        null,
      );
    return waiting?.key ?? null;
  }, [logOrder, ticket]);

  // What that block is waiting on. From the PINNED CONFIG, because the log cannot say it: an
  // unmet precondition declines silently by design. The API words the current step's, and the
  // waiting block is always on the current step.
  const waitingOn = useCallback(
    (segment: ActivitySegment) => {
      if (segment.key !== waitingKey) return null;
      const needs = ticket?.waiting_on ?? [];
      const step = pinnedConfig?.steps.find((candidate) => candidate.name === segment.nodeId);
      const calls = conditionCalls(step?.preconditions);
      return needs.length > 0 || calls.length > 0 ? { needs, calls } : null;
    },
    [waitingKey, pinnedConfig, ticket?.waiting_on],
  );

  // What that block will run on, from the pinned config for the same reason: nothing has spawned,
  // so no event names a runtime yet. Only an agent step runs on one.
  const startingRuntime = useMemo((): Spawn | null => {
    if (!ticket || waitingKey === null || !pinnedConfig) return null;
    const step = pinnedConfig.steps.find((candidate) => candidate.name === ticket.current_step);
    if (step?.kind !== "agent") return null;
    const slug = typeof pinnedConfig.runtime === "string" ? pinnedConfig.runtime : null;
    return { agent: null, runtimeSlug: slug, runtimeType: null, substrate: null };
  }, [ticket, waitingKey, pinnedConfig]);

  // The graph speaks in step names; the region speaks in attempts. Picking a node lands on that
  // step's most recent attempt, and picking the same node again walks back through its earlier
  // ones. Fed the log-ordered blocks: `attemptKeyForNode` orders the cycle by attempt number, so
  // the newest-first toggle changes what the region shows first but not what a click means.
  const activeSegment = segments.find((segment) => segment.key === activeKey) ?? null;
  const onSelectNode = useCallback(
    (id: string | null) => {
      if (!id) return;
      const key = attemptKeyForNode(logOrder, id, activeKey);
      if (key) setActiveKey(key);
    },
    [logOrder, activeKey],
  );

  const openText = useCallback(
    (content: LongContent, subtitle: string) => setText({ content, subtitle }),
    [],
  );

  if (!Number.isInteger(ticketId) || ticketId <= 0 || isNotFound(error)) {
    return (
      <div className="space-y-6">
        <PageHeader title="Ticket not found" />
        <EmptyState
          header={`Factorial can't find ticket ${Number.isInteger(ticketId) && ticketId > 0 ? ticketId : ""}`.trim()}
          description="It may belong to a project that was removed, or the link is wrong."
          primaryAction={<LinkButton to="/projects">Back to projects</LinkButton>}
        />
      </div>
    );
  }

  // Only a failure with nothing to show replaces the page: the ticket polls, and a refetch that
  // fails keeps the last answer on screen, with the header saying how old it is.
  if (!ticket) {
    return (
      <div className="space-y-6">
        <PageHeader title="" isLoading={isPending && !error} />
        {error ? (
          <ErrorMessage
            error={error}
            title="Couldn't load this ticket"
            onRetry={() => ticketQuery.refetch()}
          />
        ) : (
          <Spinner label="Loading the ticket" />
        )}
      </div>
    );
  }

  const repo = project.data?.github_repo;
  const forge = forgeHost(project.data?.ticket_source, project.data?.settings, project.data?.forge);

  const process = snapshot.data;
  const failing = (ticket.consecutive_failures ?? 0) > 0;
  const gatePr =
    condition === "waiting_gate"
      ? gatePullNumber(gatePullQuery.data?.number, entries, ticket.current_step)
      : null;
  const gatePrHref =
    condition === "waiting_gate" && gatePullQuery.data?.url ? gatePullQuery.data.url : null;
  // The gate holds on a pull request closed without a merge (decision 79).
  const gateClosed =
    condition === "waiting_gate" &&
    gatePullQuery.data?.state === "closed" &&
    gatePullQuery.data.merged === false;
  // The open run as the latest poll has it, so the drawer sees it go idle, park or finish. Only a
  // run of this ticket: a link naming another ticket's run opens nothing.
  const outputRun = runs.find((run) => run.id === outputRunId) ?? null;
  const requestLogged =
    sentBack !== null &&
    entries.some(
      (entry) =>
        entry.type === "step_changes_requested" && entry.details.reviewed_step_run_id === sentBack,
    );
  const outputHasPreview = outputRun ? hasPreview(pinnedConfig, outputRun.step_name) : false;
  const outputDeclaresFiles = outputRun ? declaresFiles(pinnedConfig, outputRun.step_name) : false;
  const review = gateReview(pinnedConfig, ticket, currentRuns);
  const refreshed = refreshNote(refresh, refreshFeedback.slow);
  const parkedRun = condition === "waiting_human" ? latestRun : null;
  const reviewRun = latestReview.ready ? latestRun : null;
  const retry = retryOffer(ticket, currentRuns, entries);
  // The held offer first: its dialog stays on its own run while an accept moves the ticket on.
  const forcing = forceHeld ?? forceOffer(ticket, condition, latestRun);
  // At a waiting gate a redo takes the step the gate reviews, offered beside the review; on any
  // other step, the step itself, offered in its latest attempt's block.
  const redoTarget = condition === "waiting_gate" ? ticket.redo_target : null;
  const redo =
    ticket.redo_target === ticket.current_step && latestRun
      ? {
          runId: latestRun.id,
          step: ticket.current_step,
          ticketId: ticket.id,
          projectId: ticket.project_id,
        }
      : undefined;

  // What the ticket's state asks of the reader. A message about the step it waits at goes in that
  // step's latest block, under its header, once the log draws that block; until then it stays at
  // the top, with the messages about the whole ticket.
  const messageProps = {
    ticket,
    condition,
    failing,
    gatePr,
    gatePrHref,
    repo,
    forge,
    onRequeue: () => requeueControl.current?.arm(),
    onOpenTerminal: parkedRun ? () => openOutput(parkedRun.id, "terminal") : undefined,
    gateClosed,
    onCloseAndExit: () => closeAndExitControl.current?.arm(),
    awaitingReview: reviewRun
      ? {
          step: reviewRun.step_name,
          openWork: hasPreview(pinnedConfig, reviewRun.step_name)
            ? {
                text: declaresFiles(pinnedConfig, reviewRun.step_name)
                  ? "Open files"
                  : "Open preview",
                onClick: () => openOutput(reviewRun.id, "preview"),
              }
            : undefined,
          onOpenTerminal: () => openOutput(reviewRun.id, "terminal"),
        }
      : null,
    review: review
      ? {
          onOpenPreview: hasPreview(pinnedConfig, review.run.step_name)
            ? () => openOutput(review.run.id, "preview")
            : undefined,
          onRequestChanges: () => {
            openOutput(
              review.run.id,
              hasPreview(pinnedConfig, review.run.step_name) ? "preview" : undefined,
            );
            setChangesOpen((count) => count + 1);
          },
          ...(redoTarget ? { redoStep: redoTarget, onRedo: () => redoControl.current?.arm() } : {}),
        }
      : null,
  } satisfies ComponentProps<typeof TicketMessages>;
  const messageKey = latestRun ? `${ticket.current_step}#${latestRun.attempt}` : null;
  const anchored =
    messageKey !== null &&
    timeline.data !== undefined &&
    segments.some((segment) => segment.key === messageKey);

  // A fill page (`routes.tsx`): exactly the window's height, and the activity panel takes what
  // the header and the graph leave, down to a floor worth reading — below that, the page scrolls.
  return (
    <div className="flex h-full flex-col gap-4">
      <PageHeader
        // The number is how people name an issue; the title is what it is about. The stored
        // observation, not a live read — the tick that admitted it already had the title — and
        // absent for a ticket that has not ticked since titles were stored. The title truncates
        // in a box of its own because an ellipsis takes the style of the box it ends, and the
        // heading's is the number's bold. The gap draws the space; the literal one keeps it in
        // the heading's text.
        title={
          <span className="flex gap-x-1.5">
            #{ticket.external_key}{" "}
            {ticket.title && (
              <span className="truncate font-normal text-subtle">{ticket.title}</span>
            )}
          </span>
        }
        fullTitle={[`#${ticket.external_key}`, ticket.title].filter(Boolean).join(" ")}
        actions={
          <TicketActions
            ticket={ticket}
            condition={condition}
            repo={repo}
            forge={forge}
            failing={failing}
            refresh={refresh}
            interruptNote={requestNote("interrupt", interrupt.data)}
            interruptError={interrupt.error}
            requeueControl={requeueControl}
            retryOffered={retry !== null}
            redoTarget={redoTarget}
            redoControl={redoControl}
            gateClosed={gateClosed}
            closeAndExitControl={closeAndExitControl}
          />
        }
        bottomBar={
          <>
            <TicketFacts ticket={ticket} condition={condition} process={process} />
            {refreshed && (
              <span role="status" className="ml-auto text-body-sm text-subtle">
                {refreshed}
              </span>
            )}
          </>
        }
        staleSince={staleSince(ticketQuery)}
      />

      <TicketMessages {...messageProps} scope={anchored ? "ticket" : undefined} />

      {/* Sticky, so the graph is still there to relate a row to whatever the page does around it.
          It sits on the page's own ground, and draws the overflow shadow ADS gives a strip that
          content scrolls under. Not on a phone, where a graph held over the activity would leave
          the log a strip to be read through. */}
      {pinnedConfig && (
        <div
          data-assist-anchor="ticket.graph"
          className="z-card -mx-1 bg-surface-sunken px-1 pt-1 pb-2 md:sticky md:top-0 md:shadow-overflow"
        >
          <Panel>
            <ProcessFlowGraph
              config={pinnedConfig}
              selectedId={activeSegment?.nodeId ?? null}
              onSelect={onSelectNode}
              // Every step that has run says how often; the one being read says which attempt.
              badges={attemptBadges(currentRuns, activeSegment)}
              currentId={ticket.current_step}
              // A finished ticket is neither live nor asking for anything, so its terminal node
              // holds still rather than beating for attention that is not wanted. An exited
              // ticket is off the floor, and it holds still for the same reason.
              currentBlinks={condition !== "done" && condition !== "exited"}
              currentRunning={latestRun?.status === "running"}
              visitedIds={visitedNodeIds(currentRuns, ticket.current_step)}
              nodeTones={tones}
              compact
            />
          </Panel>
        </div>
      )}
      <ErrorMessage error={snapshot.error} title="Couldn't load the ticket's pinned process" />

      <Panel
        title="Activity"
        data-assist-anchor="ticket.activity"
        // On a phone the page scrolls to the panel, and the panel then holds nearly the window,
        // so the log read inside it is not a slot.
        className="flex min-h-80 flex-1 flex-col max-md:min-h-[80dvh]"
        titleAction={
          // The glyph says the order the log is in; the label, like a table's sort, says what a
          // press does.
          <IconButton
            icon={order === "newest" ? "sort-down" : "sort-up"}
            label={order === "newest" ? "Show oldest first" : "Show newest first"}
            spacing="compact"
            onClick={() => setOrder(order === "newest" ? "oldest" : "newest")}
          />
        }
        actions={
          <div className="flex flex-wrap items-center justify-end gap-x-4 gap-y-2">
            {/* Facts, then what acts on the log: the order of a step's header. */}
            <TicketTotals totals={totals} />
            <Toggle label="Focused" isChecked={focused} onChange={setFocused} />
          </div>
        }
      >
        {timeline.error && !timeline.data ? (
          <ErrorMessage
            error={timeline.error}
            title="Couldn't load the activity"
            onRetry={() => timeline.refetch()}
            className="m-4"
          />
        ) : timeline.isPending ? (
          <Spinner label="Loading the activity" />
        ) : (
          <ActivityPanel
            segments={segments}
            held={held}
            order={order}
            repo={repo}
            forge={forge}
            activeKey={activeKey}
            onActiveKeyChange={setActiveKey}
            runtimeFor={runtimeFor}
            waitingOn={waitingOn}
            startingKey={waitingKey}
            startingRuntime={startingRuntime}
            onOpenText={openText}
            outputOpener={outputOpener}
            // Withheld while quarantined, like exit: the run row stays RUNNING, so the button
            // would still draw, and the route 409s the request the loop cannot read.
            onInterrupt={condition === "quarantined" ? undefined : interrupt.mutate}
            interrupting={interrupt.isPending}
            retry={
              retry && condition !== "quarantined" ? { ...retry, ticketId: ticket.id } : undefined
            }
            forceComplete={
              forcing
                ? {
                    ...forcing,
                    next: nextNeeds(pinnedConfig, forcing.run.step_name),
                    onOpenChange: (open) => setForceHeld(open ? forcing : null),
                  }
                : undefined
            }
            redo={redo}
            toLatest={requestLogged ? sentBack : null}
            focused={focused}
            message={
              anchored && messageKey
                ? {
                    key: messageKey,
                    content: (
                      <TicketMessages {...messageProps} scope="step" className="px-4 pt-3 pb-1" />
                    ),
                  }
                : undefined
            }
          />
        )}
      </Panel>

      {text && (
        <TextDrawer
          title={text.content.title}
          subtitle={text.subtitle}
          body={text.content.body}
          link={text.content.link}
          onClose={() => setText(null)}
        />
      )}
      {outputRun && (
        <OutputDrawer
          // The step, not the run: another attempt keeps the sections the reader had open.
          key={outputRun.step_name}
          ticketId={ticket.id}
          step={outputRun.step_name}
          run={outputRun}
          segment={logOrder.find((segment) => segment.run?.id === outputRun.id) ?? null}
          latest={runs.filter((run) => run.step_name === outputRun.step_name).at(-1) ?? null}
          spawn={runtimeFor(outputRun)}
          repo={repo}
          forge={forge}
          hasPreview={outputHasPreview}
          declaresFiles={outputDeclaresFiles}
          awaitsReview={stepAwaitsReview(pinnedConfig, outputRun.step_name)}
          named={named}
          reviewGate={reviewTarget(review, outputRun)}
          reviewPr={gatePr}
          changesOpen={changesOpen}
          onSelectRun={openOutput}
          onSent={(run) => setSentBack(run.id)}
          onClose={closeOutput}
        />
      )}
    </div>
  );
}

const NAMED_SECTIONS: readonly NamedSection[] = ["terminal", "preview"];

/** `params` with no run named for the Output drawer. */
function withoutOutput(params: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(params);
  for (const key of ["output", ...NAMED_SECTIONS]) next.delete(key);
  return next;
}
