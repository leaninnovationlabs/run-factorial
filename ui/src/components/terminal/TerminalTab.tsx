/**
 * A run's terminal in its Output drawer (decision 57): one terminal whose typing takes the run's
 * one writer seat, a Take over that asks first, and a reconnect when the gateway restarts or the
 * sandbox comes back.
 *
 * `GET /step-runs/{id}/terminal` says what there is before anything connects, by the rule the
 * gateway admits by: a live session (with the Waiting banner while it is IDLE), an open session, or
 * nothing to attach to — a parked session to Resume, or a kept run to Open session on. The
 * gateway stays the authority: a close it sends is read back through the same route.
 *
 * A Google session connects with its cookie. A reader signed in with the API token alone asks the
 * API for a one-use ticket before each attempt and offers it as a subprotocol, never in the URL;
 * the gateway records that reader's typing as `api-token`, since the token names no one. Signing
 * out unmounts the connection here and closes its socket.
 *
 * A reader without `tickets:terminal` is never connected and never offered Open session, because
 * the session holds the run's secrets; a parked run still offers them Resume, a ticket operation.
 */

import {
  lazy,
  type ReactNode,
  type Ref,
  Suspense,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { ApiError, api } from "../../api/client";
import { useCan } from "../../api/organizationQueries";
import { useRunTerminal, useTerminalRequests } from "../../api/queries";
import type { StepRun, TerminalInfo, TerminalParked } from "../../api/types";
import { useAuth } from "../../auth";
import { formatAbsolute } from "../../format";
import { isLive } from "../activity/runs";
import { asSentence, viewClosed } from "../activity/words";
import {
  Button,
  Code,
  ConfirmPopup,
  cx,
  ErrorMessage,
  Form,
  Icon,
  Lozenge,
  SectionMessage,
  Spinner,
  TextArea,
  Tooltip,
  VisuallyHidden,
} from "../ui";
import { CloseCode, terminalBase, terminalUrl } from "./protocol";
import { type Dial, type SocketState, type Stopped, TerminalSocket } from "./socket";
import type { Typing, XtermHandle } from "./XtermView";

const XtermView = lazy(() => import("./XtermView"));

export function TerminalTab({
  run,
  ticketId,
  latest,
  onSelectRun,
}: {
  run: StepRun;
  ticketId: number;
  /** The step's latest attempt: a rework may continue in this run's workspace. */
  latest: StepRun | null;
  onSelectRun: (stepRunId: number) => void;
}) {
  const { sessionEmail } = useAuth();
  if (!sessionEmail) return null;
  return (
    <RunTerminal
      key={run.id}
      run={run}
      ticketId={ticketId}
      me={sessionEmail}
      latest={latest}
      onSelectRun={onSelectRun}
    />
  );
}

/** Who is watching, a line each: every person once, by name, with their tabs counted. `names` is
 *  the gateway's, a name per viewer, sorted. */
export function watchers(names: readonly string[]): string {
  const tabs = new Map<string, number>();
  for (const name of names) tabs.set(name, (tabs.get(name) ?? 0) + 1);
  return [...tabs]
    .map(([name, count]) => (count > 1 ? `${name} (${count} tabs)` : name))
    .join("\n");
}

/** The count beside an eye, then who holds the seat. The count lists who is watching on hover or
 *  focus; a gateway that sends no names leaves it a number. */
function Watching({
  viewers,
  names,
  holder,
  yours,
}: {
  viewers: number;
  names: readonly string[];
  holder: string | null;
  yours: boolean;
}) {
  const typing = yours ? "you're typing" : holder === null ? null : `${holder} typing`;
  const count = (
    <span
      data-watching
      tabIndex={names.length > 0 ? 0 : undefined}
      className="inline-flex items-center gap-1 rounded-xs"
    >
      <Icon glyph="eye" />
      {viewers}
      <VisuallyHidden> watching</VisuallyHidden>
    </span>
  );
  return (
    <span role="status" className="inline-flex items-center gap-1.5 text-body-sm text-subtle">
      {names.length > 0 ? (
        <Tooltip content={watchers(names)} describes>
          {count}
        </Tooltip>
      ) : (
        count
      )}
      {typing && <span> · {typing}</span>}
    </span>
  );
}

/** The gateway's sentence for an open session (`events/terminals.py::READ_ONLY_VIEW`), for when
 *  the route could not be read. */
const READ_ONLY_VIEW = "an open session is read-only";

/** What this viewer may do with the writer seat now. */
type SeatView =
  | { kind: "unattached" }
  | { kind: "read-only"; why: string }
  | { kind: "free"; notice: string | null }
  | { kind: "claiming"; dropped: boolean }
  | { kind: "yours"; dropped: boolean }
  | { kind: "elsewhere" }
  | { kind: "held"; holder: string; taken: boolean };

function seatView(state: SocketState, readOnly: string | null, me: string): SeatView {
  if (state.phase !== "open") return { kind: "unattached" };
  const { seat } = state;
  const why = readOnly ?? (state.target === "inspect" ? READ_ONLY_VIEW : null);
  if (why !== null) return { kind: "read-only", why };
  if (seat.yours) return { kind: "yours", dropped: seat.dropped };
  if (seat.claiming) return { kind: "claiming", dropped: seat.dropped };
  if (seat.holder === null) return { kind: "free", notice: seat.notice };
  // The gateway names a person, not a connection: the same person in another tab.
  if (seat.holder === me) return { kind: "elsewhere" };
  return { kind: "held", holder: seat.holder, taken: seat.takenBy === seat.holder };
}

function typingFor(seat: SeatView): Typing {
  switch (seat.kind) {
    case "free":
    // An Escape held for a slow claim would reach the agent on the grant.
    case "claiming":
      return "free";
    case "yours":
      return "seated";
    default:
      return "off";
  }
}

/** The API's own limit on an answer (`ResumeSessionIn`). */
const MAX_ANSWER = 8000;

/** The API's sentences are clauses, as the gateway's close frames carry them. */

/** A run whose terminal cannot come back: one that had none, or whose sandbox can no longer open
 *  a session. `why` replaces the sentence: a live run's terminal is only not there yet, and a step
 *  with no agent never had one. */
export function TerminalUnavailable({ why = null }: { why?: string | null }) {
  return (
    <SectionMessage appearance="information" title="Terminal not available">
      {why
        ? asSentence(why)
        : "This run has no terminal to show. Switch on Transcript to read what it did."}
    </SectionMessage>
  );
}

const NO_TERMINAL_PERMISSION =
  "opening a run's terminal needs the tickets:terminal permission. Switch on Transcript to read what it did";

/** When an IDLE session parks, from `now`, in whole minutes rounded up. */
function minutesTo(parksAt: string, now: number): number {
  return Math.ceil((Date.parse(parksAt) - now) / 60_000);
}

function parksIn(parksAt: string, now: number): string {
  const minutes = minutesTo(parksAt, now);
  return minutes > 0 ? `in ${minutes} min` : "soon";
}

/** What becomes of an unanswered question, said so that a reader knows it can be undone. */
function pausesIn(parksAt: string, now: number): string {
  const minutes = minutesTo(parksAt, now);
  return minutes > 0
    ? `If nobody answers within ${minutes} min, the session pauses. You can resume it later.`
    : "If nobody answers soon, the session pauses. You can resume it later.";
}

function RunTerminal({
  run,
  ticketId,
  me,
  latest,
  onSelectRun,
}: {
  run: StepRun;
  ticketId: number;
  me: string;
  latest: StepRun | null;
  onSelectRun: (stepRunId: number) => void;
}) {
  // When the gateway last closed: the route may still say attachable until the loop's next pass.
  const [settling, setSettling] = useState<number | null>(null);
  const terminal = useRunTerminal(run.id, settling);
  const canTerminal = useCan("tickets:terminal");
  const info = terminal.data;
  const { refetch } = terminal;
  const kind = info?.kind;
  const attachable = info?.attachable;
  // The route caught up: what it offers changed.
  // biome-ignore lint/correctness/useExhaustiveDependencies: a change of either is the signal
  useEffect(() => setSettling(null), [kind, attachable]);
  // The ticket's poll sees the run go idle, park, resume or finish first; read what that changes.
  const status = useRef(run.status);
  useEffect(() => {
    if (status.current === run.status) return;
    status.current = run.status;
    void refetch();
  }, [run.status, refetch]);
  // A later attempt may take over this run's workspace, and with it the terminal.
  const newest = `${latest?.id}:${latest?.status}`;
  const seen = useRef(newest);
  useEffect(() => {
    if (seen.current === newest) return;
    seen.current = newest;
    void refetch({ cancelRefetch: false });
  }, [newest, refetch]);
  const onStopped = useCallback(() => {
    setSettling(Date.now());
    void refetch();
  }, [refetch]);
  // A refused claim, a freed seat or a retry may mean the ticket moved on: the route then says so.
  const reread = useCallback(() => void refetch({ cancelRefetch: false }), [refetch]);
  // A Resume or an Open session from this tab: the connection it brings takes the focus.
  const [asked, setAsked] = useState(false);
  const onAsked = useCallback(() => setAsked(true), []);
  const onFocused = useCallback(() => setAsked(false), []);
  const waiting = Boolean(info?.idle && info.attachable);

  if (!info) {
    // Unread, the gateway answers for itself: it refuses what this route would have.
    if (terminal.isError && canTerminal) {
      return (
        <Connection
          run={run}
          me={me}
          onStopped={onStopped}
          onSeatNotice={onStopped}
          onRetrying={reread}
        />
      );
    }
    if (terminal.isError) return <TerminalUnavailable why={NO_TERMINAL_PERMISSION} />;
    return <Spinner label="Checking the terminal" />;
  }
  const idle = info.idle;
  const continuedBy = info.continued_by;
  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      {/* Mounted for the tab's whole life, so going IDLE is read out once, as new text in a node
          that was already there; the countdown under it changes too often to be read out. */}
      <p role="status" className="sr-only">
        {waiting ? "The agent is waiting for you." : ""}
      </p>
      {info.attachable && canTerminal ? (
        // A fresh connection whenever what it attaches to changes: a resumed session, an open session.
        <Connection
          key={info.kind}
          run={run}
          gateway={info.gateway_origin}
          me={me}
          onStopped={onStopped}
          onSeatNotice={reread}
          onRetrying={reread}
          focusOnMount={asked}
          onFocused={onFocused}
          inspect={info.kind === "inspect"}
          readOnly={info.read_only}
          closeControl={(viewers) => (
            <CloseSession run={run} ticketId={ticketId} viewers={viewers} />
          )}
          above={(seat) =>
            info.kind === "inspect" ? (
              <OpenSessionNote keptUntil={info.kept_until} />
            ) : (
              idle && <Waiting idle={idle} seat={seat} />
            )
          }
        />
      ) : (
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto overscroll-contain px-5 py-3">
          {continuedBy !== null && (
            <ContinuedBy
              by={continuedBy}
              attempt={info.continued_by_attempt}
              latest={latest?.id === continuedBy}
              onSelectRun={onSelectRun}
            />
          )}
          {info.parked ? (
            <>
              <Parked
                run={run}
                ticketId={ticketId}
                info={info}
                parked={info.parked}
                onAsked={onAsked}
                onRefused={onFocused}
              />
              {info.enabled && canTerminal && (
                <OpenSession run={run} ticketId={ticketId} info={info} onAsked={onAsked} />
              )}
            </>
          ) : !canTerminal ? (
            <TerminalUnavailable why={NO_TERMINAL_PERMISSION} />
          ) : info.enabled &&
            !isLive(run) &&
            (info.inspect.startable || info.inspect.status === "starting") ? (
            <>
              <SectionMessage appearance="information" title="This run has ended">
                Open its session read-only to look at its workspace.
              </SectionMessage>
              <OpenSession run={run} ticketId={ticketId} info={info} onAsked={onAsked} />
            </>
          ) : isLive(run) ? (
            // A live run is not attachable only for a moment, so the gateway's reason says why.
            <TerminalUnavailable why={info.why_not} />
          ) : (
            <>
              <TerminalUnavailable />
              {/* The cause, which may be one a person can undo: a quarantine to requeue, or a
                  later attempt to open the session on instead. */}
              {info.inspect.unavailable && continuedBy === null && (
                <p className="text-body-sm text-subtle">{asSentence(info.inspect.unavailable)}</p>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

/** Where this run's terminal went. Moving there is the reader's choice: the drawer stays put, and
 *  its header already offers the way to the latest attempt. */
function ContinuedBy({
  by,
  attempt,
  latest,
  onSelectRun,
}: {
  by: number;
  attempt: number | null;
  latest: boolean;
  onSelectRun: (stepRunId: number) => void;
}) {
  return (
    <SectionMessage
      appearance="information"
      actions={
        latest || attempt === null
          ? undefined
          : [{ text: `Go to attempt ${attempt}`, onClick: () => onSelectRun(by) }]
      }
    >
      A later attempt continues in this workspace.
    </SectionMessage>
  );
}

/** Re-renders every `ms` while `on`, for a figure that counts down between polls. */
function useNow(ms: number, on: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!on) return;
    const timer = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(timer);
  }, [ms, on]);
  return now;
}

/** What the agent asked: its own text, shown as text. */
function Question({ text }: { text: string }) {
  return (
    <>
      The agent asked:
      <blockquote className="my-1 border-l-2 border-default pl-3 text-default">{text}</blockquote>
    </>
  );
}

function answering(seat: SeatView): string | null {
  switch (seat.kind) {
    case "held":
      return `${seat.holder} is answering.`;
    case "read-only":
    case "elsewhere":
      return null;
    default:
      return "Type below to answer.";
  }
}

/** An IDLE session: live, so the terminal stays below, and the way to answer is to type. */
function Waiting({ idle, seat }: { idle: NonNullable<TerminalInfo["idle"]>; seat: SeatView }) {
  const now = useNow(15_000, idle.parks_at !== null);
  return (
    <div className="px-5">
      <SectionMessage appearance="warning" title="Waiting for you">
        {idle.question ? <Question text={idle.question} /> : "The agent is waiting at its prompt. "}
        {answering(seat)}
        {idle.parks_at &&
          (idle.at_deadline !== "retry"
            ? ` ${pausesIn(idle.parks_at, now)}`
            : // Past its deadline a retry is held by running CI, which the API cannot see.
              Date.parse(idle.parks_at) <= now
              ? " If nobody types, the run ends once the step's CI settles (by the step timeout at the latest) and its step retries."
              : ` If nobody types, the run ends ${parksIn(idle.parks_at, now)} and its step retries.`)}
      </SectionMessage>
    </div>
  );
}

const PARKED_TITLE: Record<string, string> = {
  idle: "Waiting for you",
  user: "Interrupted",
  exited: "Ended from the terminal",
  rollover: "Session paused",
  ceiling: "Session time limit reached",
};

/** What the session's work still lacks: in words, or as the rules a park from before words
 *  recorded. */
function stillMissing(parked: TerminalParked): string | null {
  if (parked.needs?.length) return `Still missing: ${parked.needs.join(", ")}.`;
  if (parked.unmet?.length) return `Still missing: ${parked.unmet.join("; ")}.`;
  return null;
}

function parkedLead(parked: TerminalParked, offered: boolean, whyNot: string | null): ReactNode {
  const missing = stillMissing(parked);
  return (
    <>
      {parked.turn_error && !parked.question && (
        <>
          The agent's last reply failed:
          <blockquote className="my-1 whitespace-pre-wrap border-l-2 border-default pl-3 text-default">
            {parked.turn_error}
          </blockquote>
        </>
      )}
      {parkedReason(parked, offered, whyNot)}
      {missing && ` ${missing}`}
    </>
  );
}

function parkedReason(parked: TerminalParked, offered: boolean, whyNot: string | null): ReactNode {
  const asked = parked.question ? <Question text={parked.question} /> : null;
  if (!offered) {
    // Neither Resume nor the label would continue it: say why instead of offering either.
    return (
      <>
        {asked}
        {whyNot
          ? asSentence(whyNot)
          : "This is an earlier attempt. Only the ticket's current attempt can be resumed."}
      </>
    );
  }
  switch (parked.reason) {
    case "idle":
      return (
        <>
          {asked ? (
            <>
              {asked}
              Nobody answered, so the session paused.
            </>
          ) : (
            "The agent waited at its prompt and nobody answered, so the session paused."
          )}{" "}
          Answer here and press Resume session, or remove the <Code>factorial:waiting</Code> label
          from the issue.
        </>
      );
    case "user":
      return (
        <>
          Someone stopped this session. Press Resume session to continue it, or remove the{" "}
          <Code>factorial:interrupted</Code> label from the issue.
        </>
      );
    case "exited":
      return (
        <>
          Someone ended this session from its terminal before it was accepted. Press Resume session
          to continue it, or remove the <Code>factorial:interrupted</Code> label from the issue.
        </>
      );
    case "ceiling":
      return (
        <>
          {asked}
          The session reached its time limit and paused. Press Resume session to continue. Removing
          the <Code>factorial:interrupted</Code> label won't resume it.
        </>
      );
    default:
      return (
        <>
          The session paused and couldn't continue on its own. Press Resume session to continue, or
          remove the <Code>factorial:interrupted</Code> label from the issue.
        </>
      );
  }
}

/** A parked session: Resume continues it in its kept workspace, the answer as its next turn. A
 *  request the loop honours on the pass it wakes, so the button holds until that pass answers.
 *  The answer stays in the box until then: a refused or already-waiting Resume did not send it. */
function Parked({
  run,
  ticketId,
  info,
  parked,
  onAsked,
  onRefused,
}: {
  run: StepRun;
  ticketId: number;
  info: TerminalInfo;
  parked: TerminalParked;
  onAsked: () => void;
  /** The Resume asked for will bring no connection. */
  onRefused: () => void;
}) {
  const { resume } = useTerminalRequests(run.id, ticketId);
  const [answer, setAnswer] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const noteId = useId();
  const pending = info.resume_pending;
  const offered = info.resumable || pending;
  // A Resume that stops waiting while the session is still parked here was refused.
  const wasPending = useRef(pending);
  useEffect(() => {
    if (wasPending.current && !pending) {
      setNote("The session couldn't be resumed. The ticket's activity says why.");
      onRefused();
    }
    wasPending.current = pending;
  }, [pending, onRefused]);
  return (
    <>
      <SectionMessage appearance="warning" title={PARKED_TITLE[parked.reason] ?? "Session paused"}>
        {parkedLead(parked, offered, info.why_not_code === CloseCode.PARKED ? null : info.why_not)}
      </SectionMessage>
      {offered && (
        <Form
          aria-label="Resume the session"
          className="space-y-2"
          onSubmit={() => {
            if (pending || resume.isPending) return;
            const sent = answer.trim() ? answer : null;
            setNote(null);
            resume.mutate(sent, {
              onSuccess: (result) => {
                // `pending`: a Resume was already waiting, and the API recorded nothing of this one.
                if (result.status === "pending") {
                  setNote(
                    sent === null
                      ? "A Resume was already requested."
                      : "A Resume was already requested. Your answer wasn't sent.",
                  );
                } else {
                  onAsked();
                }
              },
            });
          }}
        >
          <TextArea
            label="Your answer"
            helperMessage="Optional. Leave it empty to reconnect and look first, or type an answer for the agent."
            maxLength={MAX_ANSWER}
            minimumRows={3}
            value={answer}
            readOnly={pending || resume.isPending}
            onChange={(event) => setAnswer(event.target.value)}
          />
          <ErrorMessage error={resume.error} title="Couldn't resume the session" />
          <div className="flex flex-wrap items-center gap-3">
            <Button
              type="submit"
              appearance="primary"
              isLoading={resume.isPending}
              aria-disabled={pending || undefined}
              aria-describedby={pending || note ? noteId : undefined}
            >
              Resume session
            </Button>
            <p id={noteId} role="status" className="text-body-sm text-subtle">
              {note ?? (pending ? "Resuming. The terminal reconnects in a moment." : "")}
            </p>
          </div>
        </Form>
      )}
    </>
  );
}

/** An open session, above its terminal: what it is and how long its workspace stays. */
function OpenSessionNote({ keptUntil }: { keptUntil: string | null }) {
  return (
    <p className="px-5 text-body-sm text-subtle">
      Read-only view of the agent's session as it ended. It closes when nobody has it open.
      {keptUntil && ` You can reopen it until ${formatAbsolute(keptUntil)}.`}
    </p>
  );
}

/** Close session: ends the open view for everyone, so a view others are watching asks first. */
function CloseSession({
  run,
  ticketId,
  viewers,
}: {
  run: StepRun;
  ticketId: number;
  viewers: number;
}) {
  const { closeSession } = useTerminalRequests(run.id, ticketId);
  const others = viewers - 1;
  const close = () => closeSession.mutate();
  // The route answers before the loop ends the view; the view going away unmounts this control.
  const closing = closeSession.isSuccess;
  const label = closing ? "Closing…" : "Close session";
  const disabledReason = closing ? "Closing the session…" : undefined;
  return (
    <div className="flex shrink-0 flex-col items-end gap-1">
      {others > 0 ? (
        <ConfirmPopup
          label={label}
          title="Close the session?"
          consequence={`${others} ${others === 1 ? "other is" : "others are"} watching. Closing ends it for everyone.`}
          confirmLabel="Close session"
          resetKey={String(run.id)}
          spacing="compact"
          isPending={closeSession.isPending}
          disabledReason={disabledReason}
          onConfirm={close}
        />
      ) : (
        <Button
          spacing="compact"
          isLoading={closeSession.isPending}
          disabledReason={disabledReason}
          onClick={close}
        >
          {label}
        </Button>
      )}
      <ErrorMessage error={closeSession.error} title="Couldn't close the session" />
    </div>
  );
}

/** Open session: a REQUEST the inspect pass honours, read back every 2 seconds while it starts. */
function OpenSession({
  run,
  ticketId,
  info,
  onAsked,
}: {
  run: StepRun;
  ticketId: number;
  info: TerminalInfo;
  onAsked: () => void;
}) {
  const { openSession } = useTerminalRequests(run.id, ticketId);
  const progress = useRef<HTMLDivElement>(null);
  const { status, reason, reason_details: reasonDetails, startable, unavailable } = info.inspect;
  const starting = status === "starting";
  // The button goes as the session starts: keep the focus with what replaced it.
  useEffect(() => {
    if (starting && openSession.isSuccess) progress.current?.focus();
  }, [starting, openSession.isSuccess]);

  if (!startable && !starting && !unavailable && !reason) return null;
  return (
    <div ref={progress} tabIndex={-1} className="space-y-2 rounded-md outline-none">
      {status === "parked" && reason && (
        <p className="text-body-sm text-subtle">
          The last open session closed: {viewClosed(reason, reasonDetails).replace(/\.$/, "")}.
        </p>
      )}
      {status === "failed" && reason && (
        <SectionMessage appearance="warning">
          The session couldn't open: {viewClosed(reason, reasonDetails).replace(/\.$/, "")}.
        </SectionMessage>
      )}
      {starting ? (
        <Spinner inline delay={0} label="Opening the session…" />
      ) : startable ? (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <Button
            isLoading={openSession.isPending}
            onClick={() => openSession.mutate(undefined, { onSuccess: onAsked })}
          >
            Open session
          </Button>
          {info.kept_until && (
            <span className="text-body-sm text-subtle">
              You can open it until {formatAbsolute(info.kept_until)}.
            </span>
          )}
        </div>
      ) : (
        unavailable &&
        unavailable !== info.why_not && (
          <p className="text-body-sm text-subtle">{asSentence(unavailable)}</p>
        )
      )}
      <ErrorMessage error={openSession.error} title="Couldn't open the session" />
    </div>
  );
}

/** True while the session cookie verifies, false once the API says it does not, null when the
 *  API could not say. */
async function probeSession(): Promise<boolean | null> {
  try {
    await api.authMe();
    return true;
  } catch (error) {
    return error instanceof ApiError && error.unauthorized ? false : null;
  }
}

function Connection({
  run,
  gateway = "",
  me,
  onStopped,
  onSeatNotice,
  onRetrying,
  focusOnMount = false,
  onFocused,
  inspect = false,
  readOnly = null,
  closeControl,
  above,
}: {
  run: StepRun;
  /** The gateway's origin, as the API names it; unread, the API's own. */
  gateway?: string;
  /** The name the gateway knows this reader by, as a seat's holder. */
  me: string;
  onStopped: () => void;
  /** The gateway refused a claim, or freed the seat, and said why. */
  onSeatNotice: () => void;
  onRetrying?: () => void;
  /** The reader asked for what this attaches to: the focus comes here. */
  focusOnMount?: boolean;
  onFocused?: () => void;
  /** An open session: what the agent left, never typed into. */
  inspect?: boolean;
  /** Why nobody may type here, from the route. */
  readOnly?: string | null;
  /** What closes an open session, told how many are attached to it. */
  closeControl?: (viewers: number) => ReactNode;
  /** What sits over the terminal, told what this viewer may do with the seat. */
  above?: (seat: SeatView) => ReactNode;
}) {
  const [state, setState] = useState<SocketState>({ phase: "connecting" });
  const [ready, setReady] = useState(false);
  const [attached, setAttached] = useState(false);
  /** From a hello until the history that follows it is drawn. */
  const [loading, setLoading] = useState(false);
  const [focused, setFocused] = useState(false);
  const view = useRef<XtermHandle>(null);
  const socket = useRef<TerminalSocket | null>(null);
  const readOnlyNote = useRef<HTMLDivElement>(null);
  const bar = useRef<HTMLDivElement>(null);
  const retrying = useRef(onRetrying);
  useEffect(() => {
    retrying.current = onRetrying;
  });

  useEffect(() => {
    if (!ready) return;
    const base = terminalBase(gateway, import.meta.env.VITE_API_BASE_URL, window.location.origin);
    const address = (cols: number, rows: number) => terminalUrl(base, run.id, cols, rows);
    const dial = (cols: number, rows: number): Dial => ({ url: address(cols, rows) });
    /** The next frame is the history that follows a hello, with tmux's first redraw. */
    let history = false;
    /** Frames that came while the history is still being drawn: xterm would parse them before
     *  the history's later slices. */
    let after: Uint8Array[] | null = null;
    const connection = new TerminalSocket({
      dial,
      size: () => view.current?.size() ?? { cols: 80, rows: 24 },
      onOutput: (bytes) => {
        if (after) {
          after.push(bytes);
          return;
        }
        if (!history) {
          view.current?.write(bytes);
          return;
        }
        history = false;
        const waiting: Uint8Array[] = [];
        after = waiting;
        view.current?.write(bytes, () => {
          for (const later of waiting) view.current?.write(later);
          after = null;
          setLoading(false);
        });
      },
      onState: setState,
      onAttach: () => {
        history = true;
        after = null;
        view.current?.reset();
        setAttached(true);
        setLoading(true);
      },
      onRetrying: () => retrying.current?.(),
      probeSession,
    });
    socket.current = connection;
    connection.start();
    return () => {
      connection.close();
      if (socket.current === connection) socket.current = null;
    };
  }, [ready, run.id, gateway]);

  // A stop is the gateway's answer to what this tab offers now; read it again.
  const stopped = state.phase === "stopped";
  useEffect(() => {
    if (stopped) onStopped();
  }, [stopped, onStopped]);

  const seat = seatView(state, readOnly, me);
  const typing = typingFor(seat);

  const notice = state.phase === "open" ? state.seat.notice : null;
  useEffect(() => {
    if (notice) onSeatNotice();
  }, [notice, onSeatNotice]);

  // Into the terminal once a first keystroke's claim is granted.
  const yours = seat.kind === "yours";
  useEffect(() => {
    if (yours) view.current?.focus();
  }, [yours]);

  // Into the terminal on the first attach when it can be typed into, but not from the drawer's
  // tabs: a reader moving between them would claim the seat with the next arrow key.
  const open = state.phase === "open";
  const greeted = useRef(false);
  useEffect(() => {
    if (!open || greeted.current) return;
    greeted.current = true;
    if (typing !== "off" && document.activeElement?.getAttribute("role") !== "tab") {
      view.current?.focus();
    }
  }, [open, typing]);

  // The socket knows the seat; only this tab knows the route's read-only sentence.
  const typable = useRef(false);
  useEffect(() => {
    typable.current = typing !== "off";
  });
  const onData = useCallback((data: string, typed: boolean) => {
    if (typable.current) socket.current?.sendInput(data, typed);
  }, []);
  const onResize = useCallback(
    (cols: number, rows: number) => socket.current?.resize(cols, rows),
    [],
  );
  const onReady = useCallback(() => setReady(true), []);
  const onLeave = useCallback(() => {
    const to =
      bar.current?.querySelector<HTMLElement>("button") ??
      readOnlyNote.current ??
      bar.current
        ?.closest('[role="dialog"]')
        ?.querySelector<HTMLElement>('button, [href], [tabindex]:not([tabindex="-1"])');
    to?.focus();
  }, []);
  useEffect(() => {
    if (!focusOnMount) return;
    // A live session takes the focus into the terminal as it attaches.
    if (inspect) readOnlyNote.current?.focus();
    onFocused?.();
  }, [focusOnMount, inspect, onFocused]);

  // The socket says once how long its wait is; the seconds count down here.
  const retry = state.phase === "retrying" ? state : null;
  // biome-ignore lint/correctness/useExhaustiveDependencies: a new retry state is a new wait
  const retryFrom = useMemo(() => Date.now(), [retry]);
  const now = useNow(1000, retry !== null);
  const secondsLeft = retry
    ? Math.min(
        Math.ceil(retry.delayMs / 1000),
        Math.max(0, Math.ceil((retryFrom + retry.delayMs - now) / 1000)),
      )
    : 0;

  const counts = state.phase === "open" ? state : null;
  // Nothing was ever drawn: an empty black box under the message would only look broken.
  const blank = state.phase === "stopped" && !attached;
  const drawing = loading && state.phase === "open";

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      {above?.(seat)}
      {/* One line from the first fit: a second line gained on attach would shrink the box under
          tmux's first draw. */}
      <div className="flex min-h-8 items-center justify-between gap-x-4 px-5 pt-2">
        <div
          ref={inspect ? readOnlyNote : undefined}
          tabIndex={inspect ? -1 : undefined}
          className="flex min-w-0 items-center gap-x-3 overflow-hidden"
        >
          {inspect && <Lozenge>Read-only</Lozenge>}
          {counts && (
            <Watching
              viewers={counts.viewers}
              names={counts.names}
              holder={counts.seat.holder}
              yours={counts.seat.yours}
            />
          )}
        </div>
        {typing !== "off" && (
          <span className="min-w-0 truncate whitespace-nowrap text-body-sm text-subtlest">
            Ctrl+] leaves the terminal.
          </span>
        )}
        {inspect && counts && closeControl?.(counts.viewers)}
      </div>
      {/* One live region for the whole life of the tab, so a change of phase is read out once,
          as new text in a node that was already there. Once attached only a screen reader has
          the text, and while retrying the eye gets the countdown, kept out of the region so it is
          not read out every second. The row keeps its height while the connection can still
          draw: a box that grew after the first draw would resize tmux's client under it. */}
      <div
        className={cx(
          "flex items-center gap-x-3 px-5 text-body-sm text-subtle",
          state.phase === "stopped" ? "sr-only" : "min-h-6",
        )}
      >
        <p role="status" className={cx((state.phase === "open" || retry) && "sr-only")}>
          {statusLine(state, drawing)}
        </p>
        {retry && (
          <>
            <span aria-hidden="true" data-countdown className="min-w-0 truncate whitespace-nowrap">
              {retry.reason}{" "}
              {secondsLeft > 0 ? `Reconnecting in ${secondsLeft} s.` : "Reconnecting."}
            </span>
            <Button
              spacing="compact"
              className="shrink-0"
              onClick={() => socket.current?.retryNow()}
            >
              Reconnect now
            </Button>
          </>
        )}
      </div>
      {state.phase === "stopped" && (
        <StoppedMessage state={state} onRetry={() => socket.current?.retryNow()} />
      )}
      <div
        className={cx(
          "relative mx-5 min-h-48 flex-1 overflow-hidden rounded-md border bg-surface-sunken p-2",
          typing !== "off" && !focused ? "border-selected ring-1 ring-selected" : "border-default",
          blank && "hidden",
        )}
      >
        <Suspense fallback={<Spinner label="Loading the terminal" />}>
          <XtermView
            ref={view}
            typing={typing}
            onData={onData}
            onResize={onResize}
            onReady={onReady}
            onLeave={onLeave}
            onFocusChange={setFocused}
          />
        </Suspense>
        {drawing && (
          // The status line above says it to a screen reader.
          <div
            aria-hidden="true"
            className="absolute inset-0 flex items-center justify-center bg-surface-sunken"
          >
            <Spinner label="Loading the conversation…" inline />
          </div>
        )}
        {typing !== "off" && !focused && (
          <span
            aria-hidden="true"
            className="pointer-events-none absolute top-2 right-2 rounded-sm bg-surface-overlay px-2 py-0.5 text-body-sm text-subtle shadow-overlay"
          >
            Click to type
          </span>
        )}
      </div>
      <SeatBar
        ref={bar}
        seat={seat}
        hidden={blank}
        onClaim={(force) => {
          socket.current?.claim(force);
          view.current?.focus();
        }}
      />
    </div>
  );
}

/** Who has the keyboard, under the terminal, and the way to take it. */
function SeatBar({
  seat,
  hidden,
  onClaim,
  ref,
}: {
  seat: SeatView;
  hidden: boolean;
  onClaim: (force: boolean) => void;
  ref: Ref<HTMLDivElement>;
}) {
  return (
    <div
      ref={ref}
      className={cx(
        "flex min-h-8 flex-wrap items-center gap-x-3 gap-y-1 px-5 pb-4 text-body-sm text-subtle",
        hidden && "hidden",
      )}
    >
      {/* Mounted for the connection's whole life, so a change of hands is read out once. */}
      <p role="status">{seatLine(seat)}</p>
      {seat.kind === "held" && (
        <ConfirmPopup
          label="Take over"
          title="Take over typing?"
          consequence={`${seat.holder} can no longer type here.`}
          confirmLabel="Take over"
          resetKey={seat.holder}
          spacing="compact"
          onConfirm={() => onClaim(true)}
        />
      )}
      {seat.kind === "elsewhere" && (
        // The reader's own other tab: nobody else loses anything, so nothing to confirm.
        <Button spacing="compact" onClick={() => onClaim(true)}>
          Type here
        </Button>
      )}
    </div>
  );
}

const NOT_SENT = "Some keys you typed while taking over weren't sent.";

function seatLine(seat: SeatView): string {
  switch (seat.kind) {
    case "read-only":
      return asSentence(seat.why);
    case "free":
      return seat.notice ? asSentence(seat.notice) : "";
    case "claiming":
      return seat.dropped ? `Taking over typing. ${NOT_SENT}` : "Taking over typing…";
    case "yours":
      return seat.dropped ? NOT_SENT : "";
    case "elsewhere":
      return "You're typing in another tab";
    case "held":
      return seat.taken ? `${seat.holder} took over typing` : `${seat.holder} is typing`;
    default:
      return "";
  }
}

const STOPPED: Record<Stopped, { title: string; tone: "information" | "warning" | "error" }> = {
  ended: { title: "The session ended", tone: "information" },
  parked: { title: "Session paused", tone: "information" },
  "signed-out": { title: "Sign in again", tone: "warning" },
  "no-terminal": { title: "Terminal not available", tone: "information" },
  forbidden: { title: "Terminal not available", tone: "warning" },
  crowded: { title: "Too many viewers", tone: "warning" },
  failed: { title: "The terminal closed", tone: "error" },
};

function statusLine(state: SocketState, loading: boolean): string {
  switch (state.phase) {
    case "connecting":
      return "Connecting…";
    case "attaching":
      return "Attaching…";
    case "retrying":
      return `${state.reason} Reconnecting.`;
    case "open":
      return loading ? "Loading the conversation…" : "Connected.";
    case "stopped":
      return `${STOPPED[state.kind].title}.`;
  }
}

export function StoppedMessage({
  state,
  onRetry,
}: {
  state: Extract<SocketState, { phase: "stopped" }>;
  onRetry: () => void;
}): ReactNode {
  const { signInAgain } = useAuth();
  const { title, tone } = STOPPED[state.kind];
  if (state.kind === "signed-out") {
    return (
      <div className="px-5">
        <SectionMessage
          appearance={tone}
          title={title}
          actions={[{ text: "Sign in again", onClick: () => void signInAgain() }]}
        >
          {state.reason}
        </SectionMessage>
      </div>
    );
  }
  // A park, an end or nothing to attach to is read back through `/terminal` as this draws, and
  // what the run offers then — Resume, Open session — takes this message's place.
  return (
    <div className="px-5">
      <SectionMessage
        appearance={tone}
        title={title}
        actions={[{ text: "Reconnect", onClick: onRetry }]}
      >
        {state.reason}
      </SectionMessage>
    </div>
  );
}
