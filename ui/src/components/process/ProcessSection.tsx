import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ApiError } from "../../api/client";
import { useCan } from "../../api/organizationQueries";
import {
  keys,
  useChangePermits,
  useImportProcesses,
  useProcessVersionAgents,
  useProcessVersionDetail,
  useSetDefaultProcess,
  useSyncProcessVersions,
} from "../../api/queries";
import type {
  ConfigAdoption,
  EditorDraftPayload,
  ProjectDetail,
  ReadinessItem,
} from "../../api/types";
import { DOCS } from "../../docs";
import { forgeHost } from "../../forge";
import { errorText, formatRelative, NO_VALUE, shortSha } from "../../format";
import { asSentence } from "../activity/words";
import { Fact, FlowFacts, isProcessConfigLike } from "../flow";
import { configurationHref } from "../project/place";
import { useFocusMode } from "../shell/focusMode";
import {
  Button,
  Code,
  ConfirmDialog,
  ErrorMessage,
  Icon,
  Lozenge,
  prefersReducedMotion,
  SectionMessage,
  ServerText,
  showFlag,
  Tab,
  TabList,
  TabPanel,
  Tabs,
  Tooltip,
  useDismissible,
} from "../ui";
import { AdoptHeadDialog } from "./AdoptHeadDialog";
import { agentsFromQuery } from "./AgentCards";
import { BranchNotice, branchState } from "./BranchNotice";
import { ConfigGallery, type GalleryChoice, type GalleryMode } from "./ConfigGallery";
import { ConfigHistory } from "./ConfigHistory";
import { LaneView } from "./LaneView";
import { withLaneName } from "./laneName";
import { ProcessEditorBody } from "./ProcessEditorBody";
import { hasPendingEdits } from "./pendingEdits";
import { withRuntimeLine } from "./runtimeLine";
import { permitting } from "./runtimePermit";
import { SyncResults } from "./SyncResults";
import { useEditorDraft } from "./useEditorDraft";

/**
 * The whole process-config section: one tab per lane, each with the graph, agents, skills and MCP
 * servers that lane's config draws, and Edit config and Sync config in the page header above. Edits are held locally, persisted per project so
 * leaving costs nothing, and "Discard edits" puts the editor back the way it opened. Versions
 * themselves are immutable: Commit and sync writes the edits to the `factorial` branch and syncs
 * that commit into the next version — never a version row directly.
 *
 * The section owns the whole `.factorial/` folder, not `process.yaml` alone, so agent health has
 * no readiness row: a checklist row could say "two agents are broken", and the explorer can say
 * which two, on the manifests themselves.
 *
 * The editor is two panes — explorer left, one file right, `process.yaml` selected first — and it
 * replaces the whole page while it is open (`onFullPage`), with its actions on top and Back to
 * return. The config is the longest thing on the page by an order of magnitude, and a drawer over
 * the section or a column beside it was too narrow to read YAML in.
 *
 * The section also owns the `factorial` branch the config lives on (`branch`, its readiness row).
 * Sync always reads that branch. With no branch and nothing synced it is the gallery a lane
 * starts from; otherwise the branch's trouble sits at the top of the lane on show.
 *
 * A reader without `projects:manage` sees every lane as it is drawn, and none of what changes it:
 * no Edit config or Sync config, no Set as default, no gallery, no draft from the assistant. View
 * config opens the editor read-only on the lane's pinned files instead.
 */
export function ProcessSection({
  project,
  branch,
  notConnected = false,
  onFullPage,
  actionsSlot,
}: {
  project: ProjectDetail;
  branch?: ReadinessItem;
  /** The project has no GitHub connection: nothing can read the branch, sync or commit, so the
   *  section says that once, with where to connect, and Edit and Sync wait for it. */
  notConnected?: boolean;
  /** Told when the editor opens and closes, so the page can hand it the whole content area. */
  onFullPage?: (on: boolean) => void;
  /** The page header's actions, where Edit config and Sync config go: they act on every lane, so
   *  they sit a level above the lanes. Absent, they lead the section; null, the slot is not
   *  mounted yet. */
  actionsSlot?: HTMLElement | null;
}) {
  /** Which lane the section is showing. A project has several, so the graph and the editor have
   *  to agree about which one they are for. Null means the project default when there is one. */
  const [chosenProcess, setProcess] = useState<string | null>(null);
  /**
   * A draft that survived a previous visit does not reopen the editor by itself — an editor that
   * takes over the page on arrival is a different thing from a pane that was still where you left
   * it.
   */
  const [editing, setEditing] = useState(false);
  /**
   * The process the editor was opened on; null is one not written yet. Held while the editor is
   * open and never read from the live project: a commit from the editor can retire that process
   * or add one that sorts before it, and an editor that followed the section's own pick would
   * show one process's text under another's name and save its drafts under the wrong key.
   */
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const canManage = useCan("projects:manage");
  const setDefault = useSetDefaultProcess(project.id);
  const [selectedNode, selectNode] = useState<string | null>(null);

  const lane =
    project.processes.find((entry) => entry.process_key === chosenProcess) ??
    project.processes.find((entry) => entry.is_default) ??
    project.processes[0];
  const processKey = lane?.process_key ?? null;
  // The row's ID, not its commit: the routes and the cache key on it, and it is shorter.
  const shown = lane?.latest;
  const detail = useProcessVersionDetail(project.id, shown?.id);
  const agents = useProcessVersionAgents(project.id, shown?.id, true);
  const graphContent = detail.data?.content ?? null;

  const [gallery, setGallery] = useState<GalleryMode | null>(null);
  /** The editor is open on a lane that does not exist yet: a gallery pick (`text`), or the draft
   *  one left behind (no `text`). */
  const [opening, setOpening] = useState<{ text?: string; title?: string } | null>(null);
  /** A skill's or MCP server's file a chip asked the editor to open on. */
  const [fileToOpen, setFileToOpen] = useState<string | null>(null);
  /** The assistant's draft the editor opens on (`useEditorDraft`); `taken` counts them, so a draft
   *  taken while the editor is open starts it afresh. */
  const [handed, setHanded] = useState<{ draft: EditorDraftPayload; taken: number } | null>(null);
  const handoff = useEditorDraft(project);
  const replacing = handoff.draft !== null && hasPendingEdits(project.id, handoff.lane);

  function takeDraft() {
    const { draft, lane } = handoff;
    if (!draft) return;
    if (lane === null) {
      setOpening({});
    } else {
      setProcess(lane);
      setOpening(null);
    }
    setFileToOpen(null);
    setHanded((previous) => ({ draft, taken: (previous?.taken ?? 0) + 1 }));
    setEditingKey(lane);
    setEditing(true);
    handoff.done();
  }
  // biome-ignore lint/correctness/useExhaustiveDependencies: a draft arriving is the event
  useEffect(() => {
    if (canManage && !notConnected && handoff.draft && !replacing) takeDraft();
  }, [handoff.draft, canManage]);

  function closeEditor() {
    setEditing(false);
    setOpening(null);
    setFileToOpen(null);
    setHanded(null);
  }

  useEffect(() => {
    onFullPage?.(editing);
  }, [editing, onFullPage]);
  useFocusMode(editing);
  // A page left with the editor open must not stay without its header.
  useEffect(() => () => onFullPage?.(false), [onFullPage]);

  const importer = useImportProcesses(project.id);
  const sync = useSyncProcessVersions(project.id);
  const [reviewing, setReviewing] = useState(false);
  const [history, setHistory] = useState(false);
  const [adoption, setAdoption] = useState<ConfigAdoption | null>(null);
  const [syncShown, dismissSync] = useDismissible(sync.data);
  const [adoptionShown, dismissAdoption] = useDismissible(adoption?.sync);
  useEffect(() => {
    if (sync.isPending) setAdoption(null);
  }, [sync.isPending]);
  const queryClient = useQueryClient();
  const reported = branchState(branch);
  // An import that just landed outranks a probe read before it: the branch exists, and the row is
  // catching up. Once the row moves, the import's word is no longer needed.
  const state = reported === "missing" && importer.isSuccess ? "checking" : reported;
  const canReview = state === "unknown-head";
  const review = canManage && canReview ? () => setReviewing(true) : undefined;
  useEffect(() => {
    if (reported !== "missing" && importer.isSuccess) importer.reset();
  }, [reported, importer]);
  // The builder commits to the branch, so with none, or one it will not build on, an editor would
  // only end in a refused commit: the notice says how to fix the branch, and Edit waits for it.
  const editBlocked = notConnected
    ? NOT_CONNECTED_BLOCK
    : state === "missing"
      ? "Create the branch first"
      : state === "unknown-head"
        ? "Adopt the head first"
        : undefined;
  // Nothing to show and nothing to edit onto: the gallery is the section until a branch exists.
  const forced = canManage && state === "missing" && project.processes.length === 0;
  const mode: GalleryMode | null = forced ? "branch-missing" : gallery;
  const permits = useChangePermits(project.id);

  /** A gallery opened or closed starts clean: an earlier attempt's failure is not this one's. */
  function showGallery(next: GalleryMode | null) {
    importer.reset();
    permits.reset();
    setGallery(next);
  }

  // The section can grow, shrink or move while the editor replaces it — a gallery becomes a lane,
  // a commit syncs a new version — so on the way back its top is put where a reader looks, unless
  // it is already there.
  const root = useRef<HTMLDivElement>(null);
  const wasEditing = useRef(false);
  useEffect(() => {
    if (editing) {
      wasEditing.current = true;
      return;
    }
    if (!wasEditing.current) return;
    wasEditing.current = false;
    // The ledger's wrapper, not this body: it carries the section's title and scroll margin.
    const anchor = root.current?.closest<HTMLElement>("[id^='readiness-']") ?? root.current;
    if (!anchor) return;
    const { top } = anchor.getBoundingClientRect();
    if (top < 0 || top > window.innerHeight / 3) {
      anchor.scrollIntoView({
        block: "start",
        behavior: prefersReducedMotion() ? "auto" : "smooth",
      });
    }
  }, [editing]);

  /** Imports what the gallery picked, committed as it stands: every lane of a template under the
   *  name it takes by default and on the runtime picked, or the default branch's folder. A lane
   *  opens in the editor afterwards for anything to change. */
  async function importChoice(choice: GalleryChoice) {
    const fromBranch = choice.kind === "default-branch";
    // Lanes already synced came from the default branch; an empty branch would retire them at
    // the next sync, so a template's lanes are added beside a copy of them instead.
    const seedFrom =
      mode !== "branch-missing"
        ? null
        : fromBranch || project.processes.length > 0
          ? "default_branch"
          : "empty";
    const processes =
      choice.kind === "template"
        ? choice.lanes.map((lane) => ({
            name: lane.name,
            content: withRuntimeLine(withLaneName(lane.yaml, lane.name), choice.runtime),
          }))
        : [];
    permits.reset();
    try {
      await importer.mutateAsync({ seed: seedFrom, processes });
    } catch {
      // The failure is drawn from `importer.error`. Most likely the branch appeared meanwhile, or
      // some lanes landed before one failed: re-read it, so the section shows what is there.
      setGallery(null);
      await queryClient.invalidateQueries({ queryKey: keys.projectReadiness(project.id) });
      return;
    }
    // Only once every lane landed: a failed import must not leave a permit behind. A refusal here
    // is drawn from `permits.error`, and the lanes stay.
    if (choice.kind === "template" && choice.permit.length > 0) {
      await permits.mutateAsync(permitting(choice.permit)).catch(() => {});
    }
    setGallery(null);
    // A copy of the default branch commits nothing of its own, so it is synced here.
    if (fromBranch) sync.mutate(null);
    const [first] = processes;
    if (first) setProcess(first.name);
  }

  // A failure stays on this mutation until the next press of Sync config. Commit and sync is a
  // different request, so an import's parse error was still on screen after the branch moved.
  // The sentence belongs to the snapshot that was showing when the request failed. Tracked here
  // rather than in `SyncMessages`, which the editor unmounts while it takes over the page.
  const syncFailure = useRef<{ error: unknown; sha: string | undefined } | null>(null);
  const recorded = syncFailure.current?.error ?? null;
  if (sync.error !== recorded) {
    syncFailure.current = sync.error ? { error: sync.error, sha: shown?.source_commit_sha } : null;
  }
  const syncError = syncFailure.current?.sha === shown?.source_commit_sha ? sync.error : null;

  const hasLane = shown !== undefined;
  const pendingNew = canManage && hasPendingEdits(project.id, null);

  const actions = (
    <ConfigActions
      canManage={canManage}
      sync={sync}
      onReview={review}
      editBlocked={notConnected ? undefined : editBlocked}
      notConnected={notConnected}
      hasLane={hasLane}
      pendingNew={pendingNew}
      pendingLane={canManage && processKey !== null && hasPendingEdits(project.id, processKey)}
      onResumeNew={() => {
        setOpening({});
        setEditingKey(null);
        setEditing(true);
      }}
      onEdit={() => {
        setEditingKey(processKey);
        setEditing(true);
      }}
      onHistory={() => setHistory(true)}
    />
  );
  const unpermittedSlugs = permits.variables?.runtime_allowlist?.add ?? [];
  // What is wrong with the branch, and what the last sync said, at the top of the lane on show.
  const notices = (
    <>
      {notConnected ? (
        <NotConnectedNotice project={project} canManage={canManage} />
      ) : (
        <BranchNotice
          row={state === "checking" ? undefined : branch}
          onCreate={canManage ? () => showGallery("branch-missing") : undefined}
          onReview={review}
          noLanes={processKey === null}
        />
      )}
      <ErrorMessage error={importer.error} className="mx-4 mt-3" />
      {permits.error && (
        <SectionMessage
          appearance="error"
          isAlert
          title={`Imported, but ${unpermittedSlugs.join(", ")} ${
            unpermittedSlugs.length === 1 ? "isn't" : "aren't"
          } permitted for this project`}
          actions={[{ text: "Open Runtime", to: `/projects/${project.id}/configuration/runtime` }]}
          className="mx-4 mt-3"
        >
          <ServerText text={errorText(permits.error)} />{" "}
          {unpermittedSlugs.length === 1
            ? "Permit it under Runtime, in Cloud sandbox runtimes, before its processes can run."
            : "Permit them under Runtime, in Cloud sandbox runtimes, before their processes can run."}
        </SectionMessage>
      )}
      <SyncMessages
        sync={sync}
        error={syncError}
        headNoticeShown={state === "unknown-head"}
        adoption={adoption ?? undefined}
        synced={{ shown: syncShown !== undefined, dismiss: dismissSync }}
        adopted={{ shown: adoptionShown !== undefined, dismiss: dismissAdoption }}
      />
    </>
  );

  return (
    <div ref={root}>
      <ErrorMessage
        error={handoff.error}
        title="Couldn't open the assistant's draft"
        className="mx-4 mt-3"
      />
      {editing ? (
        // The whole of `<main>`, edge to edge, over the page's padding: `main` is the positioned
        // box that does not scroll with the page, so the two panes get the window's full height
        // to scroll inside.
        <div className="absolute inset-0 z-10 bg-surface">
          <ProcessEditorBody
            key={handed?.taken ?? 0}
            project={project}
            processKey={editingKey}
            initialText={opening?.text}
            initialTitle={opening?.title}
            initialFile={fileToOpen ?? undefined}
            initialDraft={handed?.draft}
            readOnly={!canManage}
            heading={{
              title: `${canManage ? "Edit config" : "Config"} · ${editingKey ?? "new process"}`,
              subtitle: canManage ? (
                <span>
                  Nothing is committed to <Code>{project.github_repo}</Code> until you press Commit
                  and sync.
                </span>
              ) : (
                <span>
                  Read-only view of <Code>{project.github_repo}</Code> at the commit this process
                  runs.
                </span>
              ),
              onBack: closeEditor,
            }}
          />
        </div>
      ) : mode ? (
        <ConfigGallery
          project={project}
          mode={mode}
          busy={importer.isPending || permits.isPending}
          error={importer.error}
          onImport={importChoice}
          onCancel={forced ? undefined : () => showGallery(null)}
        />
      ) : (
        <>
          {actionsSlot === undefined ? (
            <div className="flex justify-end border-b border-default p-4">{actions}</div>
          ) : (
            actionsSlot && createPortal(actions, actionsSlot)
          )}
          {processKey === null && (
            <>
              {notices}
              <p className="p-4 text-body text-subtle">
                {notConnected && canManage && hasPendingEdits(project.id, null)
                  ? "No process yet. Your draft is kept for when GitHub is connected."
                  : !canManage || editBlocked
                    ? "No process yet."
                    : hasPendingEdits(project.id, null)
                      ? "No process yet. Your draft is waiting: press Edit config, then Commit and sync."
                      : "No process yet. Press Edit config to write one, then Commit and sync."}
              </p>
            </>
          )}
          {processKey !== null && (
            <Tabs
              id="process-lanes"
              selected={processKey}
              onChange={(key) => {
                setProcess(key);
                selectNode(null);
              }}
            >
              {/* A project may run any number of lanes: past the width, the row scrolls rather
                than wrapping or squeezing a name. */}
              <div className="overflow-x-auto px-4 pt-2 [&_[role=tab]]:shrink-0 [&_[role=tab]]:whitespace-nowrap">
                <TabList label="Processes">
                  {project.processes.map((entry) => (
                    <Tab key={entry.process_key} value={entry.process_key}>
                      <Icon glyph="lane" />
                      {entry.process_key}
                      {entry.is_default && <Lozenge isQuiet>default</Lozenge>}
                    </Tab>
                  ))}
                </TabList>
              </div>
              {/* Everything about the lane — its graph, agents, skills and MCP servers — under its tab.
                Each part rules itself off below; the panel's edge is the last one's. */}
              <TabPanel value={processKey} flush className="[&>:last-child]:border-b-0">
                {notices}
                {graphContent && isProcessConfigLike(graphContent) && (
                  <LaneView
                    config={graphContent}
                    agents={agentsFromQuery(agents)}
                    selectedId={selectedNode}
                    onSelect={selectNode}
                    onOpenTool={
                      notConnected || (canManage && editBlocked)
                        ? undefined
                        : (path) => {
                            setFileToOpen(path);
                            setEditingKey(processKey);
                            setEditing(true);
                          }
                    }
                    caption={
                      <FlowFacts>
                        <Fact
                          term="commit"
                          value={shown ? shortSha(shown.source_commit_sha) : NO_VALUE}
                        />
                        {shown?.adopted_by && <Fact term="adopted by" value={shown.adopted_by} />}
                      </FlowFacts>
                    }
                    // Where this config CAME FROM, in the bottom corner: a different question from
                    // the caption's "what is this", and one the reader consults rather than reads.
                    footerStart={
                      <FlowFacts>
                        <Fact term="ref" value={shown?.source_ref ?? "default branch"} />
                        <Fact
                          term="synced"
                          value={shown ? formatRelative(shown.activated_at) : NO_VALUE}
                        />
                      </FlowFacts>
                    }
                    // Promoting a lane acts on the one being drawn, so the control belongs on the
                    // stage that draws it, opposite the facts.
                    footerEnd={
                      canManage && !lane?.is_default ? (
                        <Tooltip content="Issues with no process label use this process" describes>
                          <Button
                            spacing="compact"
                            isLoading={setDefault.isPending}
                            onClick={() => setDefault.mutate(processKey)}
                          >
                            Set as default
                          </Button>
                        </Tooltip>
                      ) : null
                    }
                  />
                )}
                <ErrorMessage error={setDefault.error} className="mx-4 my-3" />
              </TabPanel>
            </Tabs>
          )}
        </>
      )}

      {history && <ConfigHistory project={project} onClose={() => setHistory(false)} />}
      {reviewing && (
        <AdoptHeadDialog
          projectId={project.id}
          forge={forgeHost(project.ticket_source, project.settings, project.forge)}
          onClose={() => setReviewing(false)}
          onAdopted={(result) => {
            setReviewing(false);
            sync.reset();
            setAdoption(result);
            const synced = result.sync_error === null;
            showFlag({
              appearance: synced ? "success" : "warning",
              title: `${result.adopted ? "Adopted" : "Already adopted"}${synced ? " and synced" : ", but not synced"}`,
              description: result.adopted
                ? `Recorded your adoption of ${shortSha(result.sha)}.`
                : `${shortSha(result.sha)} was already adopted. Nothing changed.`,
            });
          }}
        />
      )}
      {canManage && handoff.draft && replacing && (
        <ConfirmDialog
          title={`Replace your unsaved edits to ${handoff.lane ?? "the new process"}?`}
          consequence="The assistant's draft takes their place in the editor. Nothing is committed until you press Commit and sync."
          confirmLabel="Replace with the draft"
          appearance="warning"
          onConfirm={takeDraft}
          onCancel={handoff.done}
        />
      )}
    </div>
  );
}

/**
 * The page's actions: where the config comes from, and what to do with it — look back through
 * the synced commits and restore one, edit it, or sync the branch it lives on.
 */
function ConfigActions({
  canManage,
  sync,
  onReview,
  editBlocked,
  notConnected,
  hasLane,
  pendingNew,
  pendingLane,
  onResumeNew,
  onEdit,
  onHistory,
}: {
  /** Without it, View config alone: every other action here writes. */
  canManage: boolean;
  /** The section's, so a sync started from the gallery reports here once the gallery is gone. */
  sync: ReturnType<typeof useSyncProcessVersions>;
  /** Present while the head is a direct push someone can review and adopt. */
  onReview?: () => void;
  /** Why Edit is shut: the branch it would commit to is missing or refused. */
  editBlocked?: string;
  /** No GitHub connection: Edit and Sync are shut, and the notice under them says why once
   *  rather than a reason beside each button. */
  notConnected: boolean;
  /** A lane is synced. Without one a project can still sync, because the repo may have one. */
  hasLane: boolean;
  /** A lane not written yet has a draft: the gallery's pick, or work an earlier editor left. */
  pendingNew: boolean;
  /** The lane on show has uncommitted edits. */
  pendingLane: boolean;
  onResumeNew: () => void;
  onEdit: () => void;
  /** Opens the synced commits, and the way back to one. */
  onHistory: () => void;
}) {
  const pending = pendingLane || (!hasLane && pendingNew);
  return (
    <span className="flex flex-wrap items-center justify-end gap-2">
      <span className="text-body-sm text-subtle">
        Config is fetched from the <Code>factorial</Code> branch.
      </span>
      {!canManage && hasLane && (
        <Button disabledReason={notConnected ? NOT_CONNECTED_BLOCK : undefined} onClick={onEdit}>
          View config
        </Button>
      )}
      {canManage && (
        <>
          {pending && <Lozenge appearance="warning">Unsaved edits</Lozenge>}
          {hasLane && (
            <Button iconBefore="history" onClick={onHistory}>
              History
            </Button>
          )}
          {/* A drafted new lane beside lanes that already run has no tab of its own, so this is the
          only way back to it. */}
          {hasLane && pendingNew && (
            <Button disabledReason={editBlocked} isDisabled={notConnected} onClick={onResumeNew}>
              Edit and finalize new process
            </Button>
          )}
          <Button
            appearance={pending ? "primary" : "default"}
            disabledReason={editBlocked}
            isDisabled={notConnected}
            onClick={onEdit}
          >
            Edit config
          </Button>
          <Button
            appearance={onReview ? "warning" : pending ? "default" : "primary"}
            isLoading={sync.isPending}
            isDisabled={notConnected && !onReview}
            onClick={() => (onReview ? onReview() : sync.mutate(null))}
          >
            {onReview ? "Review and adopt" : "Sync config"}
          </Button>
        </>
      )}
    </span>
  );
}

const NOT_CONNECTED_BLOCK = "Connect GitHub first";

/** Why nothing here works yet, said once with its fix: the process config lives on a branch of
 *  the repository, and without a connection Factorial can read, sync and commit none of it. */
function NotConnectedNotice({
  project,
  canManage,
}: {
  project: ProjectDetail;
  canManage: boolean;
}) {
  return (
    <div className="border-b border-default p-4">
      <SectionMessage
        appearance="error"
        title="Connect this project to GitHub"
        actions={[
          ...(canManage
            ? [{ text: "Connect GitHub", to: configurationHref(project.id, "ticket-source") }]
            : []),
          { text: "GitHub App guide", href: `${DOCS}/github-app/` },
        ]}
      >
        Factorial reads the process config from the <Code>factorial</Code> branch of{" "}
        <Code>{project.github_repo}</Code>, and this project has no GitHub connection yet. Connect
        it with a GitHub App or a token under Settings, Tickets. Then sync a process, or write one
        with Edit config.
        {!canManage && " Ask a project admin to connect it."}
      </SectionMessage>
    </div>
  );
}

interface Dismissal {
  shown: boolean;
  dismiss: () => void;
}

/** What the last sync, or adoption, had to say. */
function SyncMessages({
  sync,
  error,
  headNoticeShown,
  adoption,
  synced,
  adopted,
}: {
  sync: ReturnType<typeof useSyncProcessVersions>;
  /** The sync's failure while the snapshot it failed on is still the one shown. */
  error: unknown;
  /** The branch notice already says why the head is refused, so that refusal is not said again. */
  headNoticeShown: boolean;
  adoption?: ConfigAdoption;
  /** Whether each success is still open, kept by the section: the editor unmounts this. */
  synced: Dismissal;
  adopted: Dismissal;
}) {
  const canRuntimes = useCan("runtimes:manage");
  const refusal = error instanceof ApiError ? error : null;
  const missing = refusal?.code === "runtime_missing" ? refusal.missingRuntimes : null;
  return (
    <div className="space-y-3 border-b border-default p-4 empty:hidden">
      {refusal?.code === "unknown_head" && headNoticeShown ? null : missing && canRuntimes ? (
        <SectionMessage
          appearance="error"
          isAlert
          actions={[
            {
              text: "Add the runtime",
              to: `/runtimes/new${missing[0] ? `?slug=${encodeURIComponent(missing[0])}` : ""}`,
            },
          ]}
        >
          <ServerText text={refusal?.message ?? ""} />
        </SectionMessage>
      ) : (
        <ErrorMessage error={error} />
      )}
      {sync.data && (
        <SyncResults result={sync.data} shown={synced.shown} onDismiss={synced.dismiss} />
      )}
      {adoption?.sync && (
        <SyncResults
          result={adoption.sync}
          adopted={adoption.adopted}
          shown={adopted.shown}
          onDismiss={adopted.dismiss}
        />
      )}
      {adoption?.sync_error && (
        <SectionMessage
          appearance="warning"
          title={
            adoption.adopted
              ? `Adopted ${shortSha(adoption.sha)}, but couldn't sync it`
              : `${shortSha(adoption.sha)} was already adopted, but the sync failed`
          }
        >
          {asSentence(adoption.sync_error)} Commit a fix on top with Edit config, then press Sync
          config.
        </SectionMessage>
      )}
    </div>
  );
}
