/**
 * A step's preview in the Output drawer (`OutputDrawer`): a tab per entry the run shows. The live
 * preview comes first when there is one: the first published URL framed with a link for each
 * other service, Start and Stop when the run's own sandbox serves it (decision 54); on the latest
 * attempt it starts by itself the first time its tab is shown, warns before an idle close with
 * Keep running, and offers Restart. Then each file its step declares (decision 71), a summary as
 * much as a spec, each framed by `FilePane`. One entry draws no tabs. Also the form that sends the
 * attempt back for changes while its review gate is deciding it.
 *
 * The live tab mounts when it is first shown and stays mounted, hidden, behind another tab, so its
 * frame and its sandbox controls keep their state, as the drawer's own sections do. A step that
 * declares only files gets a live tab once a URL is published, never an empty one. The tab chosen
 * is the drawer's, so another attempt opens on it when it has the same entry.
 *
 * A pattern gives a tab per file it matched, named by the API, and a notice with a link to the
 * rest when it matched more than it shows. A chosen tab that goes away falls back to another of
 * its pattern's before the first tab, which may be the live one and start a sandbox.
 *
 * Bound to ONE run: the drawer keys the pane on the run's id, so moving to another attempt
 * remounts the frame even when both attempts published the same URL; a reused URL may still serve
 * newer content, which the pane does not pretend otherwise about. A page that loads is not
 * evidence that the step worked, so the run's status is the drawer's to show, apart from the frame.
 *
 * The frame allows scripts, same-origin and forms. That is only safe because the API refuses a
 * URL on the dashboard's own origin (`events/previews.py`, design.md §7.9). No referrer is sent,
 * nothing of the dashboard's is forwarded, and nothing here probes the page: a site that refuses
 * framing or asks for a sign-in still opens in a new tab.
 */

import { useEffect, useId, useRef, useState } from "react";
import { newIdempotencyKey } from "../../api/idempotency";
import {
  usePreviewAction,
  useRequestChanges,
  useRunOutputs,
  useRunPreview,
} from "../../api/queries";
import type { OutputEntry, StepRun } from "../../api/types";
import { type ForgeHost, forgeName, pullName } from "../../forge";
import { shortSha } from "../../format";
import {
  Button,
  Code,
  EmptyState,
  ErrorMessage,
  Form,
  IconButton,
  LinkButton,
  panelId,
  SectionMessage,
  Select,
  type SelectEntry,
  Spinner,
  Tab,
  TabList,
  Tabs,
  TextArea,
  Tooltip,
  tabId,
  useFitsInRow,
} from "../ui";
import { FilePane } from "./FilePane";
import { hasEnded, isLive } from "./runs";
import { asSentence, changesRefused, previewStop } from "./words";

/** What the frame may do. Top navigation, downloads, popups and modal dialogs stay out: a
 *  preview must not take the dashboard's tab somewhere else or draw over it (design.md §7.9). */
const SANDBOX = "allow-scripts allow-same-origin allow-forms";

/** The API's own limit (`api/schemas.py::ChangeRequestIn`). */
const MAX_FEEDBACK = 8000;

/** The time, refreshed every `everyMs`, for the idle-close warning. */
function useNow(everyMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), everyMs);
    return () => clearInterval(id);
  }, [everyMs]);
  return now;
}

/** How long before an idle close the pane warns, and offers Keep running. */
const WARN_MS = 5 * 60_000;

/** The live preview's tab key. A declared path cannot hold `:` (the parser's character rules),
 *  so no file's key can be it. */
const LIVE = ":live";

/** The kinds `FilePane` frames. Any other is shown by its label and its links. */
const FRAMED = new Set(["file"]);

/** The pattern an entry came from: its key before `#`, which no declared path can hold. */
export function patternOf(entry: OutputEntry): string | null {
  return entry.group === null ? null : (entry.key.split("#")[0] ?? null);
}

/** The tab `selected` names, else another of its pattern's, else the first. */
function chosen<T extends { key: string }>(tabs: T[], selected: string | null): T | undefined {
  if (selected === null) return tabs[0];
  const template = selected.split("#")[0];
  return (
    tabs.find((tab) => tab.key === selected) ??
    tabs.find((tab) => tab.key === template || tab.key.startsWith(`${template}#`)) ??
    tabs[0]
  );
}

export function PreviewPane({
  run,
  step,
  forge,
  autoStart,
  selected,
  onSelect,
}: {
  run: StepRun;
  step: string;
  forge?: ForgeHost;
  /** The step's latest attempt: showing the live tab starts its preview without a click. */
  autoStart: boolean;
  /** The tab the reader chose, kept by the drawer across attempts; the first when absent. */
  selected: string | null;
  onSelect: (key: string) => void;
}) {
  const preview = useRunPreview(run.id);
  const outputs = useRunOutputs(run.id, isLive(run));
  const [liveMounted, setLiveMounted] = useState(false);
  const ids = useId();
  const data = preview.data;
  const entries = outputs.data?.entries ?? [];
  const declaresFiles = entries.some((entry) => entry.kind === "file");
  const showLive =
    data !== undefined && (data.served_by_runtime || data.urls.length > 0 || !declaresFiles);
  const tabs: { key: string; label: string; entry: OutputEntry | null }[] = [
    ...(showLive ? [{ key: LIVE, label: "Preview", entry: null }] : []),
    ...entries.map((entry) => ({ key: entry.key, label: entry.label, entry })),
  ];
  const current = chosen(tabs, selected);
  const { row, measure, fits } = useFitsInRow(tabs.map((tab) => tab.label).join("\n"));
  // Until the entries answer, the reader's file tab is not among `tabs`, and the fallback to the
  // live tab is no choice of theirs: it must neither draw nor mount the live view, whose first
  // showing starts its sandbox.
  const settled =
    !preview.isPending &&
    !outputs.isPending &&
    (outputs.data !== undefined || selected === null || selected === LIVE);
  const liveShown = settled && current?.key === LIVE;
  useEffect(() => {
    if (liveShown) setLiveMounted(true);
  }, [liveShown]);

  if (preview.isPending || outputs.isPending) {
    return <Spinner label="Checking for a preview" />;
  }
  if (preview.error && !data) {
    return (
      <ErrorMessage
        error={preview.error}
        title="Couldn't check for a preview"
        onRetry={() => preview.refetch()}
        className="m-4"
      />
    );
  }
  // A panel is named by its tab; in the dropdown no tab exists, so it is named by its label.
  const named = (key: string, label: string) =>
    fits
      ? { id: panelId(ids, key), role: "tabpanel", "aria-labelledby": tabId(ids, key) }
      : { id: panelId(ids, key), role: "region", "aria-label": label };
  const entry = current?.entry ?? null;
  const filesError = (
    <ErrorMessage
      error={outputs.error}
      title="Couldn't check for the step's files"
      onRetry={() => outputs.refetch()}
      className="m-4"
    />
  );
  if (!settled) return filesError;

  return (
    <>
      {tabs.length > 1 && current && (
        <div className="shrink-0 px-2">
          <div ref={row} data-fits="row" className="relative">
            <div
              aria-hidden="true"
              inert
              className="pointer-events-none invisible absolute top-0 left-0 h-0 w-0 overflow-hidden"
            >
              <div ref={measure} data-fits="measure" className="flex w-max gap-1 whitespace-nowrap">
                {tabs.map((tab) => (
                  <span key={tab.key} className={TAB_TEXT}>
                    <span className="max-w-60 truncate">{tab.label}</span>
                  </span>
                ))}
              </div>
            </div>
            {fits ? (
              <Tabs id={ids} selected={current.key} onChange={onSelect}>
                <TabList label={`${step} outputs`}>
                  {tabs.map((tab) => (
                    <Tab key={tab.key} value={tab.key}>
                      <Tooltip content={tab.label} onlyIfTruncated>
                        <span className="max-w-60 truncate">{tab.label}</span>
                      </Tooltip>
                    </Tab>
                  ))}
                </TabList>
              </Tabs>
            ) : (
              <EntryPicker
                label={`${step} outputs`}
                tabs={tabs}
                current={current.key}
                onSelect={onSelect}
              />
            )}
          </div>
        </div>
      )}
      {/* The last answer stays drawn through a failed poll; this only says it may be stale. */}
      {filesError}
      {showLive && (liveShown || liveMounted) && (
        <div
          {...named(LIVE, "Preview")}
          hidden={!liveShown}
          className="flex min-h-0 flex-1 flex-col"
        >
          <LivePreview preview={preview} run={run} step={step} autoStart={autoStart} />
        </div>
      )}
      {entry && (
        <div {...named(entry.key, entry.label)} className="flex min-h-0 flex-1 flex-col">
          {entry.kind === "file" && outputs.data?.cut_off && (
            <SectionMessage appearance="information" title="The record was cut off" className="m-4">
              This attempt changed more than 300 files, so its record may leave some out. A file
              named exactly still shows. A pattern shows only the files the record holds.
            </SectionMessage>
          )}
          <CappedNotice entry={entry} entries={entries} forge={forge} />
          {FRAMED.has(entry.kind) ? (
            <FilePane
              key={`${entry.key}:${entry.status}:${entry.commit ?? ""}`}
              file={entry}
              run={run}
              step={step}
              changed={outputs.data?.changed_files ?? []}
              forge={forge}
            />
          ) : (
            <EmptyState
              headingLevel={3}
              header={entry.label}
              description="This output can't be shown here."
              primaryAction={
                (entry.github_url ?? entry.url) && (
                  <LinkButton href={entry.github_url ?? entry.url ?? ""} spacing="compact">
                    Open {entry.label}
                  </LinkButton>
                )
              }
            />
          )}
        </div>
      )}
    </>
  );
}

/** The tab text's own box, so the measured copy is as wide as the tabs (`Tabs.tsx`'s `Tab`). */
const TAB_TEXT = "inline-flex h-11 items-center gap-2 border-b-2 px-3 text-body font-medium";

/**
 * The tabs as one dropdown, for a row too narrow to hold them: the same entries in the same
 * order, a pattern's files grouped under its label, with Previous and Next to read them in turn.
 */
function EntryPicker({
  label,
  tabs,
  current,
  onSelect,
}: {
  label: string;
  tabs: { key: string; label: string; entry: OutputEntry | null }[];
  current: string;
  onSelect: (key: string) => void;
}) {
  const options: SelectEntry[] = [];
  let open: string | null = null;
  for (const tab of tabs) {
    const option = { value: tab.key, label: tab.label };
    const pattern = tab.entry ? patternOf(tab.entry) : null;
    const last = options.at(-1);
    if (pattern !== null && pattern === open && last && "group" in last) {
      last.options.push(option);
    } else if (pattern !== null && tab.entry?.group) {
      options.push({ group: tab.entry.group, options: [option] });
    } else {
      options.push(option);
    }
    open = pattern;
  }
  const at = tabs.findIndex((tab) => tab.key === current);
  const before = tabs[at - 1];
  const after = tabs[at + 1];
  return (
    // The steppers wrap under the dropdown before they squeeze its name out of sight.
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-default py-1.5">
      <div className="min-w-32 flex-1">
        <Select
          label={label}
          labelHidden
          spacing="compact"
          options={options}
          value={current}
          onChange={(event) => onSelect(event.target.value)}
        />
      </div>
      <div className="flex shrink-0 items-center gap-1">
        <span data-fits="count" className="text-body-sm text-subtle">
          {at + 1} of {tabs.length}
        </span>
        <IconButton
          icon="chevron-left"
          label="Previous file"
          spacing="compact"
          // Not `disabled`: that would drop the focus of a reader stepping with the keyboard.
          aria-disabled={before === undefined}
          onClick={() => before && onSelect(before.key)}
        />
        <IconButton
          icon="chevron-right"
          label="Next file"
          spacing="compact"
          aria-disabled={after === undefined}
          onClick={() => after && onSelect(after.key)}
        />
      </div>
    </div>
  );
}

/** Said on a pattern's file when the pattern matched more files than the API shows. */
function CappedNotice({
  entry,
  entries,
  forge,
}: {
  entry: OutputEntry;
  entries: OutputEntry[];
  forge?: ForgeHost;
}) {
  const pattern = patternOf(entry);
  if (pattern === null || entry.status !== "published" || entry.group_total === null) return null;
  const shown = entries.filter((other) => patternOf(other) === pattern).length;
  if (entry.group_total <= shown) return null;
  return (
    <SectionMessage
      appearance="information"
      title={`${entry.group} matched ${entry.group_total} files`}
      actions={
        entry.group_url
          ? [{ text: `Open the folder on ${forgeName(forge)}`, href: entry.group_url }]
          : undefined
      }
      className="m-4"
    >
      The first {shown}, by path, show here.
    </SectionMessage>
  );
}

function LivePreview({
  preview,
  run,
  step,
  autoStart,
}: {
  /** The pane's own query: a second observer mounting later would refetch on mount. */
  preview: ReturnType<typeof useRunPreview>;
  run: StepRun;
  step: string;
  /** The step's latest attempt: showing it starts its preview without a click. */
  autoStart: boolean;
}) {
  const action = usePreviewAction(run.id);
  const [reloads, setReloads] = useState(0);
  const data = preview.data;

  // Decided once per mount, on the first answer: a Stop, an idle close or a renewal seen later
  // is not overruled, and a start the cap refuses is not asked again.
  const decided = useRef(false);
  useEffect(() => {
    if (!autoStart || decided.current || !data) return;
    decided.current = true;
    if (!data.served_by_runtime || !data.startable || data.renewing) return;
    if (data.status !== "none" && data.status !== "stopped") return;
    action.start.mutate();
  }, [autoStart, data, action.start]);

  const now = useNow(15_000);
  const stopsAt = data?.stops_at ? new Date(data.stops_at) : null;
  const stoppingSoon =
    data?.status === "ready" && stopsAt !== null && stopsAt.getTime() - now <= WARN_MS;

  if (preview.isPending) return <Spinner label="Checking for a preview" />;
  if (preview.error && !preview.data) {
    return (
      <ErrorMessage
        error={preview.error}
        title="Couldn't check for a preview"
        onRetry={() => preview.refetch()}
        className="m-4"
      />
    );
  }
  const [framed, ...others] = data?.urls ?? [];
  const awake = data?.status === "starting" || data?.status === "ready";

  return (
    <>
      <div className="flex flex-wrap items-center gap-2 border-b border-default px-4 py-2">
        <span className="min-w-0 flex-1 truncate font-mono text-body-sm text-subtle">
          {framed?.url ?? "No preview published"}
        </span>
        {framed && (
          <>
            <Button
              spacing="compact"
              iconBefore="refresh"
              onClick={() => setReloads((count) => count + 1)}
            >
              Reload
            </Button>
            {/* `LinkButton` opens with `rel="noreferrer"`, which implies `noopener`. */}
            <LinkButton href={framed.url} spacing="compact">
              Open in new tab
            </LinkButton>
          </>
        )}
        {others.map((link) => (
          <LinkButton key={link.url} href={link.url} spacing="compact">
            Open {link.service ?? "link"}
          </LinkButton>
        ))}
        {data?.served_by_runtime && data.startable && (
          <Button
            appearance="primary"
            spacing="compact"
            isLoading={action.start.isPending}
            onClick={() => action.start.mutate()}
          >
            Start preview
          </Button>
        )}
        {data?.restartable && (
          <Button
            spacing="compact"
            isLoading={action.restart.isPending}
            onClick={() => action.restart.mutate()}
          >
            Restart
          </Button>
        )}
        {data?.served_by_runtime && awake && (
          <Button
            spacing="compact"
            isLoading={action.stop.isPending}
            onClick={() => action.stop.mutate()}
          >
            Stop preview
          </Button>
        )}
      </div>
      {/* The last answer stays framed through a failed poll; this only says it may be stale. */}
      <ErrorMessage
        error={preview.error}
        title="Couldn't check for a newer preview"
        className="m-4"
      />
      <ErrorMessage
        error={action.start.error ?? action.restart.error ?? action.keep.error ?? action.stop.error}
        title="Couldn't send the request"
        className="m-4"
      />
      {stoppingSoon && stopsAt && (
        <SectionMessage
          appearance="warning"
          title={`Stopping at ${stopsAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`}
          actions={[{ text: "Keep running", onClick: () => action.keep.mutate() }]}
          className="m-4"
        >
          The preview stops then because nobody is using it. Press Keep running to keep it on, or
          start it again later.
        </SectionMessage>
      )}
      {data?.beside_session && data.status === "ready" && (
        <SectionMessage appearance="information" title="The agent is still working" className="m-4">
          Until it finishes, the app can't call outside services. The page works as normal in your
          browser.
        </SectionMessage>
      )}
      {data?.status === "ready" && data.uncommitted && (
        <SectionMessage
          appearance="warning"
          title="The sandbox has uncommitted changes"
          className="m-4"
        >
          This preview can show files that are not in the pull request
          {data.head_sha && (
            <>
              {" "}
              (sandbox at <Code>{shortSha(data.head_sha)}</Code>)
            </>
          )}
          . The pull request's diff is what merges.
        </SectionMessage>
      )}
      {data?.status === "failed" && data.reason && (
        <SectionMessage appearance="warning" title="The preview couldn't start" className="m-4">
          {data.restartable
            ? "Press Restart to try again."
            : data.startable
              ? "Press Start preview to try again."
              : null}
          <details className="mt-2">
            <summary className="cursor-pointer text-body-sm">Details</summary>
            <pre className="mt-1 whitespace-pre-wrap font-mono text-body-sm">{data.reason}</pre>
          </details>
        </SectionMessage>
      )}
      {framed ? (
        <iframe
          key={`${framed.url}-${reloads}`}
          src={framed.url}
          title={`${step} preview, attempt ${run.attempt}`}
          sandbox={SANDBOX}
          referrerPolicy="no-referrer"
          className="min-h-0 w-full flex-1 border-0"
        />
      ) : data?.renewing ? (
        <Spinner label="Restarting the preview" />
      ) : data?.status === "starting" ? (
        <Spinner label="Starting the preview" />
      ) : data?.served_by_runtime ? (
        <EmptyState
          headingLevel={3}
          header="The preview is not running"
          description={
            data.unavailable ??
            (data.status === "stopped" && data.reason === "idle" ? (
              "Stopped after it sat idle. Start it again when you need it."
            ) : data.status === "stopped" && data.reason ? (
              previewStop(data.reason).said
            ) : (
              <>
                Start it to serve attempt {run.attempt} of <Code>{step}</Code> from its sandbox.
              </>
            ))
          }
        />
      ) : hasEnded(run) ? (
        <EmptyState
          headingLevel={3}
          header="No preview"
          description={`Attempt ${run.attempt} didn't publish a preview.`}
        />
      ) : (
        <EmptyState
          headingLevel={3}
          header="No preview yet"
          description="The preview appears here when the agent publishes one."
        />
      )}
    </>
  );
}

/**
 * The key is made once per form and kept through a failed send, so a retry after a lost answer
 * is the same request to the API rather than a second one. A refusal is an answer: the next send
 * gets a new key.
 */
export function ChangeRequestForm({
  ticketId,
  run,
  gate,
  pr = null,
  forge,
  startOpen = false,
  onSent,
}: {
  ticketId: number;
  run: StepRun;
  gate: string;
  /** The pull request the gate decides, when known, which the text is also posted to. */
  pr?: string | null;
  forge?: ForgeHost;
  /** Open at once: the reviewer pressed Request changes before the drawer opened. */
  startOpen?: boolean;
  onSent: (run: StepRun) => void;
}) {
  const [open, setOpen] = useState(startOpen);
  const [feedback, setFeedback] = useState("");
  const [key, setKey] = useState(newIdempotencyKey);
  const [refusal, setRefusal] = useState<string | null>(null);
  const request = useRequestChanges(ticketId);

  if (!open) {
    return (
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-body-sm text-subtle">
          Attempt {run.attempt} of <Code>{run.step_name}</Code> is waiting for your review. Approve
          the {pullName(forge)} on {forgeName(forge)}, or request changes here.
        </p>
        <Button appearance="primary" onClick={() => setOpen(true)}>
          Request changes
        </Button>
      </div>
    );
  }

  return (
    <Form
      aria-label="Request changes"
      className="space-y-3"
      validate={() => feedback.trim().length > 0}
      onSubmit={() => {
        setRefusal(null);
        request.mutate(
          { stepRunId: run.id, feedback, idempotencyKey: key },
          {
            onSuccess: (result) => {
              setKey(newIdempotencyKey());
              if (result.outcome === "refused") {
                setRefusal(
                  result.reason
                    ? asSentence(changesRefused(result.reason, null, run.step_name))
                    : "No reason was given.",
                );
                return;
              }
              setFeedback("");
              setOpen(false);
              onSent(run);
            },
          },
        );
      }}
    >
      {({ attempted }) => (
        <>
          <TextArea
            label={`What should change in attempt ${run.attempt} of ${run.step_name}?`}
            isRequired
            autoFocus
            minimumRows={4}
            maxLength={MAX_FEEDBACK}
            value={feedback}
            onChange={(event) => setFeedback(event.target.value)}
            errorMessage={attempted && !feedback.trim() ? "Say what should change." : undefined}
            helperMessage={`This rejects ${gate} and starts attempt ${run.attempt + 1} of ${run.step_name}, which receives this text as its change request. The text is also posted, with your email, to ${pr ? `${pullName(forge)} #${pr}` : `the ${pullName(forge)}`}.`}
          />
          <ErrorMessage error={request.error} title="Couldn't send the change request" />
          {refusal && (
            <SectionMessage appearance="warning" title="The change request was refused" isAlert>
              {refusal}
            </SectionMessage>
          )}
          <div className="flex justify-end gap-2">
            <Button onClick={() => setOpen(false)}>Cancel</Button>
            <Button type="submit" appearance="primary" isLoading={request.isPending}>
              Send change request
            </Button>
          </div>
        </>
      )}
    </Form>
  );
}
