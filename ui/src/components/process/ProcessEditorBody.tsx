import { useEffect, useRef, useState } from "react";
import {
  useChangePermits,
  useCommitConfig,
  useConfigFile,
  useConfigFiles,
  usePreviewConfigBundle,
  useProcessTemplates,
  useValidateProcess,
} from "../../api/queries";
import type { EditorDraftPayload, ProjectDetail } from "../../api/types";
import { shortSha } from "../../format";
import {
  Code,
  CodeEditor,
  ErrorMessage,
  IconButton,
  SectionMessage,
  showFlag,
  useCopy,
} from "../ui";
import { commitButtonState, unconfirmedStaleCount } from "./commitGate";
import { EditorActions, type EditorHeading } from "./EditorActions";
import { EditorMessages } from "./EditorMessages";
import { readBundleDraft, reconcile, writeBundleDraft } from "./explorer/bundleDraft";
import { ConfigTree } from "./explorer/ConfigTree";
import {
  type CreatedFiles,
  newPathProblem,
  readCreatedFiles,
  writeCreatedFiles,
} from "./explorer/createdFiles";
import { FilePane } from "./explorer/FilePane";
import { NewEntry } from "./explorer/NewEntry";
import {
  type RepoEdits,
  readRepoEdits,
  repoLockReason,
  staleRepoEdits,
  toConfigEdits,
  withEdit,
  withoutEdit,
  writeRepoEdits,
} from "./explorer/repoEdits";
import {
  type ExplorerFile,
  lanePath,
  mergeFiles,
  toggled,
  topLevelExpanded,
} from "./explorer/tree";
import { type FileTab, FileTabs } from "./FileTabs";
import { laneNameFor, laneNameIn, withLaneName } from "./laneName";
import { clearDraft } from "./processDraft";
import { withRuntimeLine } from "./runtimeLine";
import { permitsToAdd, permitting } from "./runtimePermit";
import { draftEdits, draftFiles } from "./useEditorDraft";
import { useProcessText } from "./useProcessText";
import { useProcessValidation } from "./useProcessValidation";
import { useRuntimeChoice } from "./useRuntimeChoice";
import { useStoredDraft } from "./useStoredDraft";

/** The process file's tab: its path moves when the YAML renames the lane, and its tab must not. */
const PROCESS_TAB = "";

/**
 * The drafting half: explorer left, the open files right, one tab each. A template prefills the
 * YAML, every file is yours to edit, and nothing commits until the operator presses Commit and
 * sync, which writes every edited file to the repo's `factorial` branch in one commit and syncs
 * it. Closing a tab only puts its file away; the edit stays held for that commit.
 *
 * The preview runs itself rather than hiding behind a button, because the explorer is on screen
 * the whole time and an explorer that is empty until you click something is a worse explorer.
 * Consent did not move with it: the ticks are still what decides which files the commit
 * carries, and editing the config still clears them.
 *
 * `readOnly` shows the pinned commit's files to a reader who may not commit, and keeps no draft.
 */
export function ProcessEditorBody({
  project,
  processKey = null,
  initialText,
  initialTitle,
  initialFile,
  initialDraft,
  onPreviewContent,
  heading,
  readOnly = false,
}: {
  project: ProjectDetail;
  /** Which lane is being edited. `null` is a lane that does not exist yet — the drafts key on
   *  it either way, so two lanes' edits can never restore into each other. */
  processKey?: string | null;
  /** Text a gallery pick starts the editor on. Applied once, after seeding, so it wins over the
   *  shared new-lane draft. */
  initialText?: string;
  /** The pick's name, for the flag that says a draft was replaced. */
  initialTitle?: string;
  /** The file to open on instead of the process file, by its path under the config folder. */
  initialFile?: string;
  /** An assistant's draft (`useEditorDraft`): its text and files replace the stored draft once
   *  seeding lands. The section asks first when that would replace unsaved edits. */
  initialDraft?: EditorDraftPayload;
  /** The parsed echo of the current text when valid, else null — the section draws it. */
  onPreviewContent?: (content: Record<string, unknown> | null) => void;
  /** What is being edited and the way back, at the start of the action bar over both panes. */
  heading?: EditorHeading;
  readOnly?: boolean;
}) {
  const templates = useProcessTemplates();
  const validate = useValidateProcess(project.id);
  const commit = useCommitConfig(project.id);
  const permits = useChangePermits(project.id);

  const { text, setText, raw, pinned, baseline, seededSha, dirty, rebase } = useProcessText(
    project,
    processKey,
    readOnly,
  );
  const { copy } = useCopy();

  const appliedInitial = useRef(false);
  // biome-ignore lint/correctness/useExhaustiveDependencies: applied once, the moment seeding lands
  useEffect(() => {
    if (!initialText || appliedInitial.current || baseline === null) return;
    appliedInitial.current = true;
    const previous = text;
    setText(initialText);
    if (previous.trim() && previous !== initialText) {
      showFlag({
        appearance: "info",
        title: `Replaced your unsaved new-process draft with ${initialTitle ?? "the template"}`,
        actions: [{ text: "Undo", onClick: () => setText(previous) }],
      });
    }
  }, [baseline]);
  const importInput = useRef<HTMLInputElement>(null);

  const { runtime, registered } = useRuntimeChoice(project.id, pinned?.id, text, !readOnly);

  // --- the file explorer: what a commit from `text` would create, and what the operator
  // has picked and edited out of it ------------------------------------------------------------

  const { verdict, validForText, checkingValidity, checkFailed } = useProcessValidation(
    validate,
    readOnly ? "" : text,
  );

  // The explorer is on screen the whole time the editor is open, so the preview runs itself
  // rather than waiting for a click: as soon as the text validates, ask the server what a pull
  // request from it would write. The query is keyed on `content` itself
  // (`usePreviewConfigBundle`), so an edit needs no explicit invalidation — the key for the NEW
  // text simply has no cached answer yet, and the tree shows the previous preview's files until one
  // lands. Consent is still the ticks, and an edit still clears them (the effect below).
  const preview = usePreviewConfigBundle(
    project.id,
    text,
    !readOnly && validForText && text.trim().length > 0,
  );
  const previewed = preview.isSuccess && !preview.isPlaceholderData;
  const previewFiles = preview.data?.files ?? [];

  const bundleDraft = useStoredDraft(
    () => (readOnly ? {} : (readBundleDraft(project.id, processKey) ?? {})),
    (next) => !readOnly && writeBundleDraft(project.id, processKey, next),
  );
  const repoEdits = useStoredDraft<RepoEdits>(
    () => (readOnly ? {} : readRepoEdits(project.id, processKey)),
    (next) => !readOnly && writeRepoEdits(project.id, processKey, next),
  );
  const appliedDraft = useRef(false);
  /** The commit an assistant's draft was written against, when it is not the one this editor
   *  started from: its process text replaces the whole file, so a change made since would be
   *  undone by committing it unread. */
  const [draftBase, setDraftBase] = useState<string | null>(null);
  // biome-ignore lint/correctness/useExhaustiveDependencies: applied once, the moment seeding lands
  useEffect(() => {
    if (!initialDraft || appliedDraft.current || baseline === null) return;
    appliedDraft.current = true;
    if (initialDraft.base_sha && seededSha && initialDraft.base_sha !== seededSha) {
      setDraftBase(initialDraft.base_sha);
    }
    setText(initialDraft.content);
    bundleDraft.write(draftFiles(initialDraft));
    repoEdits.write(draftEdits(initialDraft));
  }, [baseline]);
  const repoEditCount = Object.keys(repoEdits.data).length;
  const createdFiles = useStoredDraft<CreatedFiles>(
    () => (readOnly ? {} : readCreatedFiles(project.id, processKey)),
    (next) => !readOnly && writeCreatedFiles(project.id, processKey, next),
  );
  const createdCount = Object.keys(createdFiles.data).length;
  /** Folders made here that hold no file yet: git keeps no empty folder, so neither does a
   *  reload. */
  const [folders, setFolders] = useState<string[]>([]);
  const [making, setMaking] = useState<"file" | "folder" | null>(null);
  // The body each edited file is compared against to tell a genuine edit from one that simply
  // has not been refreshed yet (`bundleDraft.ts`'s `reconcile`). Anchored once, the moment a file
  // first diverges, and never touched again while the edit stands — updating it on every preview
  // would compare an edit to itself and never notice a fact that moved underneath it.
  const pristine = useRef<Record<string, string>>({});
  // Only a preview of the text on screen may drop an edit or call it stale. Until one lands, every
  // held edit stands as written; otherwise the next keystroke saves a draft without the others.
  const { draft, stale: staleList } = previewed
    ? reconcile(bundleDraft.data, previewFiles, pristine.current)
    : { draft: bundleDraft.data, stale: [] as string[] };
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-running only when a NEW preview of the text on screen lands is the intent; `draft`/`write` change identity every render and would make this fire forever
  useEffect(() => {
    if (previewed) bundleDraft.write(draft);
  }, [preview.data, previewed]);

  const [selectedOverride, setSelectedOverride] = useState<Set<string> | null>(null);
  const [expandedOverride, setExpandedOverride] = useState<Set<string> | null>(null);
  // Paths whose staleness the operator has explicitly accepted with "Use anyway" — cleared
  // whenever the underlying edit is reverted or a fresh config invalidates the whole preview.
  const [confirmed, setConfirmed] = useState<Set<string>>(new Set());
  /** The files open as tabs, and the one on show; null until the operator opens or closes one,
   *  so the first tab can wait for the listing to say where the config folder is. */
  const [tabState, setTabState] = useState<{ open: string[]; active: string | null } | null>(null);

  // What the repo already has at the pinned commit, merged with what a commit would add:
  // one node per path, so "already in the repo" is a property of the file rather than of which
  // list it came from. A new lane has no pin of its own, so it reads the branch through a lane
  // that has one: the tree then shows what the new lane lands beside. Absent for a project with
  // no version yet — then the tree is proposals only.
  const listedFrom =
    pinned ??
    (processKey === null
      ? ((project.processes.find((lane) => lane.is_default) ?? project.processes[0])?.latest ??
        null)
      : null);
  const listing = useConfigFiles(project.id, listedFrom?.id);
  const configDir = listing.data?.config_dir ?? ".factorial";
  // Where the commit will WRITE this lane, which is always the folder layout — the legacy
  // root file is read for ever and never written again (decision 37). The name comes from the
  // text being edited rather than from the lane being edited, because renaming the process in
  // the YAML moves the file, and the server derives the path the same way.
  const draftName = typeof verdict?.content?.name === "string" ? verdict.content.name : processKey;
  const processPath = lanePath(configDir, draftName ?? "<name>");
  const merged = mergeFiles(listing.data?.files ?? [], previewFiles);
  // The lane's own file is never a proposal, so a lane the branch does not have yet would be
  // missing from the tree — the one file the commit always writes.
  const laneIsNew = !merged.some((file) => file.path === processPath);
  const newPaths = Object.keys(createdFiles.data).filter(
    (path) => !merged.some((file) => file.path === path),
  );
  const explorerFiles = [
    ...merged,
    ...(laneIsNew && !readOnly ? [{ path: processPath }] : []),
    ...newPaths.map((path) => ({ path })),
  ].sort((a, b) => a.path.localeCompare(b.path));
  // A bundle draft an earlier build kept for a repo file is never sent: that file's edit is held
  // in `repoEdits`.
  const listed = new Set((listing.data?.files ?? []).map((file) => file.path));
  const edited = new Set(Object.keys(draft).filter((path) => !listed.has(path)));
  // Judged only against a listing that has loaded: an empty one would call every edit stale.
  const repoStale = listing.data ? staleRepoEdits(repoEdits.data, explorerFiles) : [];
  const staleSet = new Set([...staleList, ...repoStale]);

  // Ticked by default: every candidate file that is not already in the repo. `propose()` only
  // ever contributes a body for the variant a lane actually matches, so this is already the
  // `default_on` set — nothing here decides fitness a second time.
  const defaultSelected = new Set(
    previewFiles.filter((f) => !f.present_in_repo).map((f) => f.path),
  );
  const selected = selectedOverride ?? defaultSelected;
  const expanded = expandedOverride ?? topLevelExpanded(explorerFiles);

  // Consent belongs to the exact bytes it was given about: a changed `process.yaml` may propose
  // different files, so the ticks and the "use anyway" acceptances go with it.
  const lastText = useRef(text);
  useEffect(() => {
    if (lastText.current === text) return;
    lastText.current = text;
    setSelectedOverride(null);
    setExpandedOverride(null);
    setConfirmed(new Set());
  }, [text]);

  function toggleSelected(paths: string[], on: boolean) {
    setSelectedOverride((prev) => {
      const next = new Set(prev ?? defaultSelected);
      for (const path of paths) {
        if (on) next.add(path);
        else next.delete(path);
      }
      return next;
    });
  }

  function toggleExpanded(path: string, open: boolean) {
    setExpandedOverride((prev) => toggled(prev ?? expanded, path, open));
  }

  function editFile(path: string, body: string) {
    const proposed = previewFiles.find((f) => f.path === path);
    if (!(path in pristine.current)) {
      pristine.current = { ...pristine.current, [path]: draft[path]?.body ?? proposed?.body ?? "" };
    }
    bundleDraft.write({
      ...draft,
      [path]: { body, facts: proposed?.facts ?? draft[path]?.facts ?? {} },
    });
  }

  function revertFile(path: string) {
    const next = { ...draft };
    delete next[path];
    bundleDraft.write(next);
    const nextPristine = { ...pristine.current };
    delete nextPristine[path];
    pristine.current = nextPristine;
    setConfirmed((prev) => {
      if (!prev.has(path)) return prev;
      const next = new Set(prev);
      next.delete(path);
      return next;
    });
  }

  // Scoped to the selection: a stale file the operator has unticked is not being submitted, so
  // it must not block the button — only its own row still shows the badge. A stale repo edit is
  // always part of the commit, so it always counts; keeping it rebases it.
  const unconfirmedStale = unconfirmedStaleCount(staleList, selected, confirmed) + repoStale.length;

  const firstTab = initialFile ? `${configDir}/${initialFile}` : PROCESS_TAB;
  const tabs = tabState ?? { open: [firstTab], active: firstTab };
  const pathOf = (key: string) => (key === PROCESS_TAB ? processPath : key);
  const isProcess = tabs.active === PROCESS_TAB;
  const openPath = tabs.active === null ? "" : pathOf(tabs.active);

  function openTab(path: string) {
    const key = path === processPath ? PROCESS_TAB : path;
    setTabState((prev) => {
      const open = (prev ?? tabs).open;
      return { open: open.includes(key) ? open : [...open, key], active: key };
    });
  }

  function closeTab(key: string) {
    setTabState((prev) => {
      const { open, active } = prev ?? tabs;
      const at = open.indexOf(key);
      const rest = open.filter((other) => other !== key);
      // The neighbour on the right takes the place of the tab on show, as an editor's tabs do.
      return {
        open: rest,
        active: active === key ? (rest[Math.min(at, rest.length - 1)] ?? null) : active,
      };
    });
  }
  // Repo files the commit would rewrite: the lane's process text while it differs from the pin,
  // and every held repo edit.
  const modified = new Set([
    ...(dirty && !laneIsNew ? [processPath] : []),
    ...Object.keys(repoEdits.data),
  ]);
  const created = new Set([...(laneIsNew ? [processPath] : []), ...newPaths]);
  const isCreated = openPath in createdFiles.data;
  // Anything a reset would undo: the config text, an edited body, a moved tick.
  const changed =
    dirty || edited.size > 0 || selectedOverride !== null || repoEditCount > 0 || createdCount > 0;
  const openExplorerFile: ExplorerFile = explorerFiles.find((f) => f.path === openPath) ?? {
    path: openPath,
  };
  // A file the pinned listing (or the head) has is the repo's, never a proposal body — even when
  // the catalog also proposes that path — so it opens on the repo's own copy.
  const openRepo = openExplorerFile.repo;
  const repoFile = useConfigFile(
    project.id,
    listedFrom?.id,
    tabs.active !== null && !isProcess && openRepo?.sha ? openPath : null,
  );
  const openLock = openRepo ? repoLockReason(openRepo) : undefined;
  // The catalog proposes a path the branch has, but no listing vouches for the repo's copy: the
  // template is not that copy, so the file is shown for what it is and never edited.
  const unreadable = !openRepo && Boolean(openExplorerFile.proposal?.present_in_repo);
  const unreadableReason = !listedFrom
    ? "Already on the factorial branch. Commit this process first, then edit it from its own copy."
    : "Already on the factorial branch. Factorial couldn't read its copy.";

  function editRepoFile(path: string, body: string) {
    const base = explorerFiles.find((f) => f.path === path)?.repo?.sha;
    if (!base || repoFile.data === undefined) return;
    repoEdits.write(
      withEdit(repoEdits.data, path, body, { original: repoFile.data.content, base }),
    );
  }

  function keepRepoEdit(path: string) {
    const base = explorerFiles.find((f) => f.path === path)?.repo?.sha;
    const held = repoEdits.data[path];
    if (!base || !held) return;
    repoEdits.write({ ...repoEdits.data, [path]: { ...held, base } });
  }

  /** Everything a reset puts back, held so the flag's Undo can restore it exactly. */
  function snapshotEdits() {
    return {
      text,
      draft,
      repoEdits: repoEdits.data,
      createdFiles: createdFiles.data,
      pristine: pristine.current,
      selectedOverride,
      expandedOverride,
      confirmed,
    };
  }

  function restore(saved: ReturnType<typeof snapshotEdits>) {
    // Marked as already seen, so the text's return is not read as a new edit that drops the
    // ticks and acceptances being put back with it.
    lastText.current = saved.text;
    setText(saved.text);
    bundleDraft.write(saved.draft);
    repoEdits.write(saved.repoEdits);
    createdFiles.write(saved.createdFiles);
    pristine.current = saved.pristine;
    setSelectedOverride(saved.selectedOverride);
    setExpandedOverride(saved.expandedOverride);
    setConfirmed(saved.confirmed);
  }

  /** Back to the pinned config, with every edit, tick and acceptance dropped: the state the
   *  editor opens in. It does not close — an operator undoing their edits is still working. It
   *  applies at once and offers Undo (decision 50): a local draft is cheaper to restore than to
   *  guard with a dialog over the editor. */
  function discardEdits() {
    const saved = snapshotEdits();
    clearDraft(project.id, processKey);
    bundleDraft.write({});
    repoEdits.write({});
    for (const path of Object.keys(createdFiles.data)) closeTab(path);
    createdFiles.write({});
    pristine.current = {};
    setText(baseline ?? "");
    setSelectedOverride(null);
    setExpandedOverride(null);
    setConfirmed(new Set());
    showFlag({
      appearance: "info",
      title: "Discarded your edits",
      description: "The editor shows the synced config again.",
      actions: [{ text: "Undo", onClick: () => restore(saved) }],
    });
  }

  function replaceWithTemplate(templateKey: string, laneKey: string) {
    const template = templates.data?.templates.find((entry) => entry.key === templateKey);
    const lane = template?.lanes.find((entry) => entry.key === laneKey);
    if (!template || !lane) return;
    const previous = text;
    // An existing lane keeps its own name, so the commit replaces that lane rather than adding
    // the template's beside it. A lane not yet written keeps the name it was given, or takes one
    // no lane has: the template's own may be a lane that already runs.
    const taken = project.processes.map((lane) => lane.process_key);
    const named = withLaneName(
      lane.yaml,
      processKey ?? laneNameIn(text) ?? laneNameFor(lane.key, taken),
    );
    // The text's slug, not the one the template names: replacing the text must not change the
    // runtime the lane runs on (decision 36).
    setText(withRuntimeLine(named, runtime));
    showFlag({
      appearance: "info",
      title: `Replaced the config with ${template.title}`,
      actions: [{ text: "Undo", onClick: () => setText(previous) }],
    });
  }

  useEffect(() => {
    onPreviewContent?.(verdict?.valid && verdict.content ? verdict.content : null);
  }, [verdict, onPreviewContent]);

  // A referenced agent with no manifest on the branch gets its stub from a commit, so a commit
  // that would write only that stub is still worth making.
  const agentsMissing = (preview.data?.components ?? []).some(
    (component) => component.kind === "agent" && component.referenced && !component.present_in_repo,
  );
  const unchanged =
    !dirty && selected.size === 0 && repoEditCount === 0 && createdCount === 0 && !agentsMissing;
  const commitButton = commitButtonState({
    hasText: text.trim().length > 0,
    checkingConfig: checkingValidity,
    checkFailed,
    checkingRepo: preview.isFetching || (repoEditCount > 0 && listing.isPending),
    committing: commit.isPending,
    unchanged,
    validForText,
    previewed,
    unconfirmedStale,
  });
  const needPermit = permitsToAdd(project, runtime ? [runtime] : [], registered);

  function commitNow() {
    commit.mutate(
      {
        content: text,
        files: [...selected].map((path) => ({
          path,
          body: draft[path]?.body ?? previewFiles.find((f) => f.path === path)?.body ?? "",
        })),
        edits: toConfigEdits(repoEdits.data),
        created: Object.entries(createdFiles.data).map(([path, body]) => ({ path, body })),
      },
      {
        onSuccess: (result, sent) => {
          // The commit landed whether or not its sync did, so a path it wrote leaves the
          // drafts, unless the operator has typed since: then the held body is newer than
          // the one committed and stays. A path it did not write stays too, and the
          // message below names it. A no-op commit wrote nothing because the branch already
          // holds every sent body, so those leave the drafts as if written.
          const sentBody = new Map(
            [...sent.files, ...sent.edits, ...(sent.created ?? [])].map((file) => [
              file.path,
              file.body,
            ]),
          );
          const written = new Set(result.committed ? result.written : sentBody.keys());
          const unwritten = <T extends { body: string }>(held: Record<string, T>) =>
            Object.fromEntries(
              Object.entries(held).filter(
                ([path, file]) => !written.has(path) || sentBody.get(path) !== file.body,
              ),
            );
          let kept: Record<string, unknown> = {};
          bundleDraft.update((current) => {
            kept = unwritten(current);
            return kept as typeof current;
          });
          repoEdits.update(unwritten);
          createdFiles.update((current) =>
            Object.fromEntries(
              Object.entries(current).filter(
                ([path, body]) => !written.has(path) || sentBody.get(path) !== body,
              ),
            ),
          );
          pristine.current = Object.fromEntries(
            Object.entries(pristine.current).filter(([path]) => path in kept),
          );
          setSelectedOverride(null);
          setExpandedOverride(null);
          setConfirmed(new Set());
          // A failed sync leaves the lane on its old version, so the baseline stays too.
          if (result.sync_error === null) rebase(sent.content, result.sha);
        },
      },
    );
  }

  const fileTabs: FileTab[] = tabs.open.map((key) => {
    const path = pathOf(key);
    return {
      key,
      path,
      modified:
        key === PROCESS_TAB
          ? dirty
          : edited.has(path) || path in repoEdits.data || path in createdFiles.data,
    };
  });
  const editedCount = (dirty ? 1 : 0) + edited.size + repoEditCount + createdCount;
  const openFolder = openPath.includes("/") ? openPath.slice(0, openPath.lastIndexOf("/")) : "";
  const startIn = openFolder.startsWith(`${configDir}/`)
    ? `${openFolder.slice(configDir.length + 1)}/`
    : "";

  function create(path: string) {
    setMaking(null);
    const parents = path.split("/").map((_, end, all) => all.slice(0, end + 1).join("/"));
    // Open down to it, and a new folder itself, so what was just made is in view.
    const opened = making === "folder" ? parents : parents.slice(0, -1);
    setExpandedOverride((prev) =>
      opened.reduce((set, dir) => toggled(set, dir, true), prev ?? expanded),
    );
    if (making === "folder") {
      setFolders((prev) => [...prev, path]);
      return;
    }
    setFolders((prev) => prev.filter((folder) => !path.startsWith(`${folder}/`)));
    createdFiles.write({ ...createdFiles.data, [path]: "" });
    openTab(path);
  }

  function deleteCreated(path: string) {
    const { [path]: _, ...rest } = createdFiles.data;
    createdFiles.write(rest);
    closeTab(path);
  }

  return (
    // Two panes that scroll independently cannot live inside one scrolling column, and the
    // explorer has to stay put while a long file moves: the caller gives this a bounded height.
    <div className="flex h-full min-h-0 flex-col">
      <EditorActions
        readOnly={readOnly}
        heading={heading}
        commitButton={commitButton}
        onCommit={commitNow}
        unpermittedSlugs={needPermit}
        onPermitAndCommit={() => permits.mutate(permitting(needPermit), { onSuccess: commitNow })}
        text={text}
        checkingValidity={checkingValidity}
        verdict={verdict}
        editedCount={editedCount}
        unchanged={unchanged}
        changed={changed}
        onDiscard={discardEdits}
        templates={templates.data?.templates ?? []}
        onTemplate={replaceWithTemplate}
        onCopy={() => copy(text)}
        importInput={importInput}
        onImport={setText}
      />
      {/* Everything the commit button has to say sits under it, above both panes, so neither
        pane moves when one of them appears. */}
      <EditorMessages
        committed={commit.data}
        sent={[
          ...(commit.variables?.files ?? []),
          ...(commit.variables?.edits ?? []),
          ...(commit.variables?.created ?? []),
        ]}
        requestError={commit.error ?? validate.error ?? raw.error ?? preview.error}
        verdict={verdict}
      />
      {draftBase && seededSha && (
        <SectionMessage
          appearance="warning"
          title="The assistant's draft was written against another commit"
          className="mx-4 my-3"
        >
          It was drafted from <Code>{shortSha(draftBase)}</Code>, and this editor started from{" "}
          <Code>{shortSha(seededSha)}</Code>. Its process file replaces the whole file, so compare
          it with what the process runs before you press Commit and sync.
        </SectionMessage>
      )}
      <div className="flex min-h-0 flex-1 flex-col md:flex-row">
        <div className="max-h-64 shrink-0 overflow-y-auto border-b border-default p-3 md:max-h-none md:w-72 md:border-r md:border-b-0">
          <div className="mb-2 flex items-center justify-between gap-2">
            <span className="text-heading-xxs text-subtle">Files</span>
            {!readOnly && (
              <span className="flex gap-1">
                <IconButton
                  icon="plus"
                  label="New file"
                  spacing="compact"
                  isSelected={making === "file"}
                  onClick={() => setMaking(making === "file" ? null : "file")}
                />
                <IconButton
                  icon="folder"
                  label="New folder"
                  spacing="compact"
                  isSelected={making === "folder"}
                  onClick={() => setMaking(making === "folder" ? null : "folder")}
                />
              </span>
            )}
          </div>
          {making && (
            <NewEntry
              key={making}
              kind={making}
              configDir={configDir}
              start={startIn}
              problemOf={(path) =>
                newPathProblem(
                  path,
                  making,
                  configDir,
                  explorerFiles.map((file) => file.path),
                  folders,
                )
              }
              onCreate={create}
              onCancel={() => setMaking(null)}
            />
          )}
          <ConfigTree
            files={explorerFiles}
            expanded={expanded}
            selected={selected}
            edited={edited}
            stale={staleSet}
            modified={modified}
            created={created}
            openPath={openPath}
            onToggle={readOnly ? undefined : toggleSelected}
            onExpand={toggleExpanded}
            folders={folders}
            onOpen={openTab}
          />
          <ErrorMessage error={listing.error} />
        </div>

        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          <FileTabs
            tabs={fileTabs}
            active={tabs.active}
            onSelect={(key) => setTabState({ open: tabs.open, active: key })}
            onClose={closeTab}
          />
          <div className="min-h-0 flex-1">
            {tabs.active === null ? (
              <p className="p-4 text-body text-subtle">
                {readOnly
                  ? "No file is open. Choose one in the explorer."
                  : "No file is open. Choose one in the explorer. Closing a file keeps its edits for the next commit."}
              </p>
            ) : isProcess ? (
              // `process.yaml` is not a bundle file: `assemble.py` commits it from `yaml_text` and
              // never from the selection, so its text stays the editor's own rather than becoming a
              // proposal body that could diverge from the config that was parsed and version checked.
              <CodeEditor
                label="process.yaml"
                value={text}
                onChange={readOnly ? undefined : setText}
                readOnly={readOnly}
                placeholder={
                  readOnly ? undefined : "Start from a template, or paste an existing process file."
                }
                className="h-full"
              />
            ) : isCreated ? (
              <FilePane
                file={openExplorerFile}
                body={createdFiles.data[openPath] ?? ""}
                stale={false}
                isNew
                onChange={(body) => createdFiles.write({ ...createdFiles.data, [openPath]: body })}
                revertLabel="Delete this new file"
                onRevert={() => deleteCreated(openPath)}
              />
            ) : openRepo ? (
              <FilePane
                file={openExplorerFile}
                body={repoEdits.data[openPath]?.body ?? repoFile.data?.content ?? ""}
                loading={
                  !(openPath in repoEdits.data) && Boolean(openRepo.sha) && repoFile.isPending
                }
                stale={staleSet.has(openPath)}
                lockReason={openLock}
                revertLabel="Revert to the repository's copy"
                onChange={
                  !readOnly && !openLock && repoFile.data
                    ? (body) => editRepoFile(openPath, body)
                    : undefined
                }
                onRevert={
                  openPath in repoEdits.data
                    ? () => repoEdits.write(withoutEdit(repoEdits.data, openPath))
                    : undefined
                }
                onConfirm={readOnly || openLock ? undefined : () => keepRepoEdit(openPath)}
              />
            ) : unreadable ? (
              <FilePane
                file={openExplorerFile}
                body=""
                loading={Boolean(listedFrom) && listing.isPending}
                stale={false}
                lockReason={unreadableReason}
              />
            ) : (
              <FilePane
                file={openExplorerFile}
                body={draft[openPath]?.body ?? openExplorerFile.proposal?.body ?? ""}
                stale={staleSet.has(openPath) && !confirmed.has(openPath)}
                onChange={readOnly ? undefined : (body) => editFile(openPath, body)}
                onRevert={readOnly ? undefined : () => revertFile(openPath)}
                onConfirm={
                  readOnly ? undefined : () => setConfirmed((prev) => new Set(prev).add(openPath))
                }
              />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
