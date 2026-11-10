/**
 * A run's output in the one drawer every reading surface opens in (decision 50): its Terminal and,
 * when the step declares one, its Preview, side by side. A preview that holds the files the step
 * declares is called Files. Each collapses to a bar in its own place,
 * so the order never changes; a bar expands its section again. Full width shows one section alone.
 * The last one open stays open. A link that names the terminal opens it alone; otherwise both open.
 *
 * Every run has a Terminal section, even one that never had a terminal: it says so there, and its
 * Transcript switch shows the run's transcript in the terminal's place. The section opens on the
 * terminal. The terminal stays mounted behind the transcript, so switching back keeps the
 * connection and the writer seat; the transcript mounts afresh each time, so a live one opens at
 * its foot.
 *
 * A section mounts when it first expands and stays mounted through a collapse, hidden in its own
 * place, so a collapsed terminal keeps its connection and a collapsed preview keeps its page. A
 * section never changes parent, only its slot's width: moving a frame in the page would reload it.
 * The emulator's chunk is fetched only once the terminal is first shown.
 *
 * A phone holds one section at a time: two side by side there would each be a column too narrow
 * for a terminal's lines or a page. A link that names one opens that one, and expanding a bar
 * collapses the section that was open. A window that narrows to a phone shows only the first
 * section that was open.
 *
 * Keyed on the step by its caller, not on the run: moving to another attempt keeps what the
 * reader had open, the Preview's tab too. An attempt that arrives as a live agent run opens its
 * terminal, with the transcript switched off.
 */

import { type ReactNode, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { useRunActivity } from "../../api/queries";
import type { StepRun } from "../../api/types";
import type { ForgeHost } from "../../forge";
import { runShown } from "../../status";
import { TerminalTab, TerminalUnavailable } from "../terminal/TerminalTab";
import {
  Button,
  CopyButton,
  cx,
  Drawer,
  type DrawerHandle,
  EmptyState,
  Icon,
  IconButton,
  Pressable,
  StatusLozenge,
  TextDrawer,
  Toggle,
} from "../ui";
import { useIsPhone } from "../ui/media";
import type { ActivitySegment, LongContent, Spawn } from "./activity";
import { EventRow } from "./EventRow";
import { ChangeRequestForm, PreviewPane } from "./PreviewPane";
import { RunRuntime } from "./RunRuntime";
import { isLive } from "./runs";
import { sessionReference, TranscriptBody, transcriptText } from "./Transcript";
import { outcomeWords } from "./words";

/** A section a link can name: `?terminal=` and `?preview=`. */
export type NamedSection = "terminal" | "preview";

const LABEL: Record<NamedSection, string> = {
  terminal: "Terminal",
  preview: "Preview",
};

/** The sections a run has, in the order they sit. */
function outputSections(hasPreview: boolean): NamedSection[] {
  return hasPreview ? ["terminal", "preview"] : ["terminal"];
}

export function OutputDrawer({
  ticketId,
  step,
  run,
  segment,
  latest,
  spawn,
  repo,
  forge,
  hasPreview,
  declaresFiles,
  named,
  reviewGate,
  reviewPr,
  changesOpen,
  onSelectRun,
  onSent,
  onClose,
}: {
  ticketId: number;
  step: string;
  run: StepRun;
  /** The run's block in the activity, for a step with no transcript. */
  segment: ActivitySegment | null;
  /** The step's newest run, so a reader on an older attempt can get to it. */
  latest: StepRun | null;
  spawn: Spawn | undefined;
  repo: string | null | undefined;
  forge?: ForgeHost;
  /** The step's pinned config declares a preview. */
  hasPreview: boolean;
  /** That preview declares files: the section is called Files. */
  declaresFiles: boolean;
  /** `terminal` opens the terminal alone; otherwise every section opens. */
  named?: NamedSection;
  /** The gate that can send `run` back now (`reviewTarget`), or null. */
  reviewGate: string | null;
  /** The pull request the reviewing gate decides, for the form's wording. */
  reviewPr: string | null;
  /** Non-zero opens the change request form at once; a new value opens it again. */
  changesOpen: number;
  onSelectRun: (stepRunId: number) => void;
  /** A change request went. The drawer slides shut by itself, then calls `onClose`. */
  onSent: (run: StepRun) => void;
  onClose: () => void;
}) {
  const sections = outputSections(hasPreview);
  const phone = useIsPhone();
  const [open, setOpen] = useState<NamedSection[]>(() =>
    named === "terminal"
      ? ["terminal"]
      : phone
        ? [named && sections.includes(named) ? named : "terminal"]
        : sections,
  );
  const [mounted, setMounted] = useState<ReadonlySet<NamedSection>>(() => new Set(open));
  const [transcript, setTranscript] = useState(false);
  const [entry, setEntry] = useState<string | null>(null);
  const [text, setText] = useState<LongContent | null>(null);
  const shownRun = useRef(run.id);
  useEffect(() => {
    if (run.id === shownRun.current) return;
    shownRun.current = run.id;
    if (run.kind === "agent" && isLive(run)) {
      setOpen((before) =>
        phone ? ["terminal"] : before.includes("terminal") ? before : [...before, "terminal"],
      );
      setTranscript(false);
    }
  }, [run, phone]);

  const opened = sections.filter((section) => open.includes(section));
  const expanded = phone ? opened.slice(0, 1) : opened;
  const show = (next: NamedSection[]) => {
    setOpen(next);
    setMounted((before) => new Set([...before, ...expanded, ...next]));
  };
  const toggle = (section: NamedSection) =>
    show(
      expanded.includes(section)
        ? expanded.filter((s) => s !== section)
        : phone
          ? [section]
          : [...expanded, section],
    );
  const ids = useId();
  const idOf = (section: NamedSection) => `${ids}-${section}`;
  const drawer = useRef<DrawerHandle>(null);

  const runId = run.runtime_run_id;
  const activity = useRunActivity(run.id, Boolean(runId), isLive(run));
  const data = activity.data;
  // A run that just finished stops polling: read its turns and outcome once more.
  const { refetch } = activity;
  const status = useRef(run.status);
  useEffect(() => {
    if (status.current === run.status) return;
    status.current = run.status;
    if (runId) void refetch();
  }, [run.status, runId, refetch]);

  return (
    <Drawer
      title={`${step}, attempt ${run.attempt}`}
      subtitle={
        <>
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <StatusLozenge status={runShown(run)} />
            {latest && latest.id !== run.id && (
              <Button spacing="compact" onClick={() => onSelectRun(latest.id)}>
                Go to the latest attempt ({latest.attempt})
              </Button>
            )}
            {runId && <RunRuntime spawn={spawn} model={run.model} withAgent />}
          </span>
          {runId && (
            <RunFacts
              reference={sessionReference(spawn?.substrate ?? undefined, runId)}
              runId={runId}
              facts={[
                data && `${data.turns.length} turns`,
                data?.outcome && outcomeWords(data.outcome),
                data?.truncated && "Showing the first part of a long transcript",
              ]}
            />
          )}
        </>
      }
      width="full"
      bare
      handle={drawer}
      footer={
        reviewGate && (
          <ChangeRequestForm
            key={changesOpen}
            ticketId={ticketId}
            run={run}
            gate={reviewGate}
            pr={reviewPr}
            forge={forge}
            startOpen={changesOpen > 0}
            onSent={(sent) => {
              drawer.current?.close();
              onSent(sent);
            }}
          />
        )
      }
      onClose={onClose}
    >
      <div className="flex h-full min-h-0">
        {sections.map((section, index) => (
          <Section
            key={section}
            id={idOf(section)}
            label={section === "preview" && declaresFiles ? "Files" : LABEL[section]}
            expanded={expanded.includes(section)}
            first={index === 0}
            alone={expanded.length === 1}
            mounted={mounted.has(section) || expanded.includes(section)}
            onToggle={() => toggle(section)}
            onFullWidth={() => show([section])}
            actions={
              section === "terminal" && (
                <>
                  {transcript && data && data.turns.length > 0 && (
                    <CopyButton
                      value={transcriptText(data.turns, data.milestones)}
                      name="transcript"
                    />
                  )}
                  <Toggle label="Transcript" isChecked={transcript} onChange={setTranscript} />
                </>
              )
            }
          >
            {section === "preview" ? (
              <PreviewPane
                key={run.id}
                run={run}
                step={step}
                forge={forge}
                autoStart={run.id === latest?.id}
                selected={entry}
                onSelect={setEntry}
              />
            ) : (
              <>
                <div hidden={transcript} className="flex min-h-0 flex-1 flex-col">
                  {run.kind === "agent" ? (
                    <TerminalTab
                      key={run.id}
                      run={run}
                      ticketId={ticketId}
                      latest={latest}
                      onSelectRun={onSelectRun}
                    />
                  ) : (
                    <div className="px-5 py-3">
                      <TerminalUnavailable
                        why={
                          isLive(run)
                            ? "this run has no terminal. Switch on Transcript to follow it"
                            : "This step has no terminal."
                        }
                      />
                    </div>
                  )}
                </div>
                {transcript && (
                  <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
                    {runId ? (
                      <TranscriptBody key={run.id} run={run} />
                    ) : segment && segment.entries.length > 0 ? (
                      <ol className="py-2">
                        {segment.entries.map((entry) => (
                          <EventRow
                            key={entry.id}
                            entry={entry}
                            repo={repo}
                            forge={forge}
                            held={null}
                            onOpenText={setText}
                          />
                        ))}
                      </ol>
                    ) : (
                      <EmptyState
                        headingLevel={3}
                        header="No activity recorded for this attempt yet"
                      />
                    )}
                  </div>
                )}
              </>
            )}
          </Section>
        ))}
      </div>
      {text && (
        <TextDrawer
          title={text.title}
          body={text.body}
          link={text.link}
          onClose={() => setText(null)}
        />
      )}
    </Drawer>
  );
}

/** What the subtitle says of a run's transcript. Where its log sits on the server is for an
 *  operator, so it is copied rather than shown. */
function RunFacts({
  reference,
  runId,
  facts,
}: {
  reference: string | null;
  runId: string;
  facts: (string | false | null | undefined)[];
}) {
  const shown = facts.filter((fact): fact is string => Boolean(fact));
  return (
    <span className="flex flex-wrap items-center gap-x-2">
      {shown.length > 0 && <span>{shown.join(" · ")}</span>}
      {reference ? (
        <CopyButton value={reference} label="Copy log path" />
      ) : (
        <CopyButton value={runId} label="Copy run id" />
      )}
    </span>
  );
}

/**
 * One section in its own slot: a header with its name between Collapse and Full width, both drawn
 * only while another section is open too. Collapsed, the slot narrows to a bar where the section
 * stood and the section is hidden inside it, so what it holds survives and nothing moves. The
 * control pressed goes away, so the focus moves to what took its place: the bar, Collapse, or the
 * section itself once it is alone.
 */
function Section({
  id,
  label,
  expanded,
  first,
  alone,
  mounted,
  onToggle,
  onFullWidth,
  actions,
  children,
}: {
  id: string;
  label: string;
  expanded: boolean;
  /** The leftmost slot, which needs no rule before it. */
  first: boolean;
  /** The only open section, which neither collapses nor widens. */
  alone: boolean;
  mounted: boolean;
  onToggle: () => void;
  onFullWidth: () => void;
  actions?: ReactNode;
  children: ReactNode;
}) {
  const bar = useRef<HTMLButtonElement>(null);
  const collapse = useRef<HTMLButtonElement>(null);
  const body = useRef<HTMLElement>(null);
  const refocus = useRef(false);
  // biome-ignore lint/correctness/useExhaustiveDependencies: a change of either is the signal
  useLayoutEffect(() => {
    if (!refocus.current) return;
    refocus.current = false;
    (expanded ? (collapse.current ?? body.current) : bar.current)?.focus();
  }, [expanded, alone]);
  const moving = (act: () => void) => () => {
    refocus.current = true;
    act();
  };

  return (
    <div
      className={cx(
        "flex min-h-0 min-w-0 overflow-hidden transition-[flex-grow,flex-basis] duration-250 ease-inout-bold motion-reduce:transition-none",
        expanded ? "grow basis-0" : "grow-0 basis-9",
        !first && "border-l border-default",
      )}
    >
      {!expanded && (
        <Pressable
          ref={bar}
          aria-expanded={false}
          aria-controls={id}
          onClick={moving(onToggle)}
          className="focus-inset flex w-full flex-col items-center gap-2 py-3 hover:bg-neutral-subtle-hovered"
        >
          <Icon glyph="chevron-right" />
          <span className="text-body-sm font-medium [writing-mode:vertical-rl]">{label}</span>
        </Pressable>
      )}
      <section
        ref={body}
        id={id}
        aria-label={label}
        hidden={!expanded}
        tabIndex={-1}
        className="flex min-h-0 min-w-0 flex-1 flex-col outline-none"
      >
        <div className="flex h-10 shrink-0 items-center gap-1 border-b border-default px-2">
          {!alone && (
            <IconButton
              ref={collapse}
              icon="chevron-left"
              label={`Collapse ${label}`}
              spacing="compact"
              aria-expanded
              aria-controls={id}
              onClick={moving(onToggle)}
            />
          )}
          <h3 className="px-1 text-body-sm font-semibold text-default">{label}</h3>
          {!alone && (
            <IconButton
              icon="maximize"
              label={`Expand ${label} to full width`}
              spacing="compact"
              onClick={moving(onFullWidth)}
            />
          )}
          <div className="ml-auto flex items-center gap-1">{actions}</div>
        </div>
        <div className="flex min-h-0 flex-1 flex-col">{mounted && children}</div>
      </section>
    </div>
  );
}
