import { type CSSProperties, useEffect, useRef, useState } from "react";
import {
  useChangePermits,
  useCheckConfig,
  useCommitConfig,
  useConfigFile,
  useConfigFiles,
  usePreviewConfigBundle,
  useProcessTemplates,
  useValidateProcess,
} from "../../api/queries";
import type { EditorDraftPayload, ProjectDetail } from "../../api/types";
import { shortSha } from "../../format";
import { usePointEntries } from "../assistant/AssistantProvider";
import {
  Code,
  CodeEditor,
  ConfirmDialog,
  ContextMenu,
  DropdownMenu,
  ErrorMessage,
  IconButton,
  type MenuEntry,
  PopupAt,
  SectionMessage,
  Skeleton,
  showFlag,
  useCopy,
  VisuallyHidden,
} from "../ui";
import { commitButtonState, unconfirmedStaleCount } from "./commitGate";
import { EditorActions, type EditorHeading } from "./EditorActions";
import { EditorMessages } from "./EditorMessages";
import { readBundleDraft, reconcile, writeBundleDraft } from "./explorer/bundleDraft";
import { ConfigTree } from "./explorer/ConfigTree";
import {
  type Construct,
  constructFiles,
  constructFolder,
  constructIcon,
  constructLabel,
  constructName,
  constructNameProblem,
  constructsFor,
  isEmptyManifest,
} from "./explorer/constructs";
import {
  type CreatedFiles,
  newPathProblem,
  readCreatedFiles,
  writeCreatedFiles,
} from "./explorer/createdFiles";
import {
  type DeletedFiles,
  deletablePaths,
  readDeletedFiles,
  settledDeletes,
  staleDeletes,
  toConfigDeletes,
  writeDeletedFiles,
} from "./explorer/deletedFiles";
import { FilePane } from "./explorer/FilePane";
import { NewEntry } from "./explorer/NewEntry";
import { PaneResizer } from "./explorer/PaneResizer";
import { clampWidth, MIN_WIDTH, readPaneWidth, writePaneWidth } from "./explorer/paneWidth";
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
  laneOf,
  lanePath,
  mergeFiles,
  pathsUnder,
  type TreeNode,
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

/** What the explorer's prompt is asking for: a path for a file or folder (started in `start`,
 *  or beside the open file when null), or a construct's name. */
type Making = ({ kind: "file" | "folder"; start: string | null } | { construct: Construct }) & {
  /** Where the person pressed to add: the prompt floats there. */
  at: Point;
};

type Point = { x: number; y: number };

/** Just under an element: where a prompt opened from a button goes. */
function below(element: Element | null): Point {
  const box = element?.getBoundingClientRect();
  return box ? { x: box.left, y: box.bottom } : { x: 16, y: 16 };
}

/** The words of the prompt for each construct: its example name, and what its button says. */
const NAMING: Record<Construct["kind"], { placeholder: string; submit: string }> = {
  agent: { placeholder: "review-agent", submit: "Create agent" },
  mcp: { placeholder: "tracker", submit: "Create MCP server" },
  process: { placeholder: "fast", submit: "Create process" },
  skill: { placeholder: "review-notes", submit: "Create skill" },
};

const AGENT_MANIFEST = /\/agents\/([^/]+)\/agent\.yaml$/;
const fileNameOf = (path: string) => path.slice(path.lastIndexOf("/") + 1);

/**
 * The drafting half: explorer left, the open files right, one tab each. A template prefills the
 * YAML, every file is yours to edit, and nothing commits until the operator presses Commit and
 * sync, which writes every edited file to the repo's `factorial` branch in one commit, removes
 * every file staged for deletion, and syncs it. Closing a tab only puts its file away; the edit
 * stays held for that commit.
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
  const check = useCheckConfig(project.id);
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
  // The draft carries its own text and files, so it goes on screen at once: waiting for the
  // pinned file left the editor empty for as long as that read took (#374). The seed only fills
  // an empty editor (`useProcessText`), so it cannot take the draft's place when it lands.
  // biome-ignore lint/correctness/useExhaustiveDependencies: applied once, as the editor opens
  useEffect(() => {
    if (!initialDraft || appliedDraft.current) return;
    appliedDraft.current = true;
    setText(initialDraft.content);
    bundleDraft.write(draftFiles(initialDraft));
    repoEdits.write(draftEdits(initialDraft));
  }, []);
  // Which commit the editor started from is known only once the pinned file is read. Judged
  // once, against that commit: a commit made here moves `seededSha` on, and the draft is no
  // older for it.
  const draftJudged = useRef(false);
  useEffect(() => {
    if (draftJudged.current || !seededSha) return;
    draftJudged.current = true;
    if (initialDraft?.base_sha && initialDraft.base_sha !== seededSha) {
      setDraftBase(initialDraft.base_sha);
    }
  }, [initialDraft, seededSha]);
  /** The pinned file is being read and nothing else gave the editor its text. Not merely
   *  "not seeded yet": a file the cache already holds seeds in the same tick, and a skeleton
   *  drawn for that one frame would be a flicker. */
  const loading = baseline === null && !initialDraft && raw.isLoading;
  const repoEditCount = Object.keys(repoEdits.data).length;
  const createdFiles = useStoredDraft<CreatedFiles>(
    () => (readOnly ? {} : readCreatedFiles(project.id, processKey)),
    (next) => !readOnly && writeCreatedFiles(project.id, processKey, next),
  );
  const createdCount = Object.keys(createdFiles.data).length;
  const deletedFiles = useStoredDraft<DeletedFiles>(
    () => (readOnly ? {} : readDeletedFiles(project.id, processKey)),
    (next) => !readOnly && writeDeletedFiles(project.id, processKey, next),
  );
  const deletedCount = Object.keys(deletedFiles.data).length;
  /** A commit that deletes a process file waits here for a second press. */
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  /** Folders made here that hold no file yet: git keeps no empty folder, so neither does a
   *  reload. */
  const [folders, setFolders] = useState<string[]>([]);
  const [making, setMaking] = useState<Making | null>(null);
  const panes = useRef<HTMLDivElement>(null);
  const filePanel = useRef<HTMLDivElement>(null);
  const [paneWidth, setPaneWidth] = useState(readPaneWidth);
  /** The width as last asked for, for the write that follows a key press in the same event. */
  const keptWidth = useRef(paneWidth);
  const panesWidth = () => panes.current?.getBoundingClientRect().width ?? 0;
  function resizePane(width: number) {
    keptWidth.current = clampWidth(width, panesWidth());
    setPaneWidth(keptWidth.current);
  }
  const makingKind = making && "kind" in making ? making.kind : null;
  /** The folder whose menu a right-click opened, and where the pointer was. */
  const [menu, setMenu] = useState<{ node: TreeNode; at: { x: number; y: number } } | null>(null);
  // The tree's menu takes the right-click from the dashboard's own, so it carries the assistant's
  // two entries itself.
  const pointEntries = usePointEntries();
  const assistantEntries = (node: TreeNode): MenuEntry[] => {
    const group = pointEntries(() => ({
      label: node.name,
      text: node.path,
      page: null,
      file: node.path,
    }));
    return group ? [group] : [];
  };
  const shells = templates.data?.shells ?? {};
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
  const deleteStale = listing.data ? staleDeletes(deletedFiles.data, explorerFiles) : [];
  // A staged deletion whose file has already left the branch is done. Judged only against a
  // listing that has loaded, like the stale ones.
  const settled = listing.data ? settledDeletes(deletedFiles.data, explorerFiles).join("\n") : "";
  // biome-ignore lint/correctness/useExhaustiveDependencies: `settled` is the event; the draft's writer changes identity every render
  useEffect(() => {
    if (settled) restorePaths(settled.split("\n"));
  }, [settled]);
  const staleSet = new Set([...staleList, ...repoStale, ...deleteStale]);
  const deletable = deletablePaths(explorerFiles, Object.keys(createdFiles.data));
  const deletedSet = new Set(Object.keys(deletedFiles.data));
  /** The process on screen is staged for deletion: the commit writes no file for it. */
  const retiring = processPath in deletedFiles.data;
  const retiredProcesses = Object.keys(deletedFiles.data)
    .map((path) => laneOf(configDir, path))
    .filter((name): name is string => name !== null);

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
  // always part of the commit, so it always counts; keeping it rebases it. A stale deletion
  // counts too: restoring the file is how it is cleared.
  const unconfirmedStale =
    unconfirmedStaleCount(staleList, selected, confirmed) + repoStale.length + deleteStale.length;

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
    dirty ||
    edited.size > 0 ||
    selectedOverride !== null ||
    repoEditCount > 0 ||
    createdCount > 0 ||
    deletedCount > 0;
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
  const openDeleted = openPath in deletedFiles.data;
  // The catalog proposes a path the branch has, but no listing vouches for the repo's copy: the
  // template is not that copy, so the file is shown for what it is and never edited.
  const unreadable = !openRepo && Boolean(openExplorerFile.proposal?.present_in_repo);
  const unreadableReason = !listedFrom
    ? "Already on the factorial branch. Commit this process first, then edit it from its own copy."
    : "Already on the factorial branch. Factorial couldn't read its copy.";

  function editRepoFile(path: string, body: string) {
    const base = explorerFiles.find((f) => f.path === path)?.repo?.sha;
    // A file staged for deletion is not also edited: the server refuses a path sent as both.
    if (!base || repoFile.data === undefined || path in deletedFiles.data) return;
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
      deletedFiles: deletedFiles.data,
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
    deletedFiles.write(saved.deletedFiles);
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
    deletedFiles.write({});
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

  function replaceWithTemplate(templateKey: string) {
    const template = templates.data?.templates.find((entry) => entry.key === templateKey);
    // The editor holds one lane at a time, so a template with several gives its first.
    const lane = template?.lanes[0];
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
    !dirty &&
    selected.size === 0 &&
    repoEditCount === 0 &&
    createdCount === 0 &&
    deletedCount === 0 &&
    !agentsMissing;
  const commitButton = commitButtonState({
    loading,
    hasText: text.trim().length > 0,
    checkingConfig: checkingValidity,
    checkFailed,
    checkingRepo:
      preview.isFetching || ((repoEditCount > 0 || deletedCount > 0) && listing.isPending),
    committing: commit.isPending,
    unchanged,
    validForText,
    previewed,
    unconfirmedStale,
  });
  const needPermit = permitsToAdd(project, runtime ? [runtime] : [], registered);

  /** Everything a commit carries. Validate sends the same, so its verdict is the commit's. */
  function draftToSend() {
    return {
      content: text,
      // A commit that deletes this process adds no file for it; the server refuses one.
      files: retiring
        ? []
        : [...selected].map((path) => ({
            path,
            body: draft[path]?.body ?? previewFiles.find((f) => f.path === path)?.body ?? "",
          })),
      edits: toConfigEdits(repoEdits.data),
      created: Object.entries(createdFiles.data).map(([path, body]) => ({ path, body })),
      deleted: toConfigDeletes(deletedFiles.data),
    };
  }

  // A verdict belongs to the draft it judged: any change to what a commit would carry drops it.
  const { reset: dropCheck } = check;
  // biome-ignore lint/correctness/useExhaustiveDependencies: each dependency is a part of the draft, and a change to any of them is the event
  useEffect(() => {
    dropCheck();
  }, [
    text,
    bundleDraft.data,
    repoEdits.data,
    createdFiles.data,
    deletedFiles.data,
    selectedOverride,
    dropCheck,
  ]);

  /** Opens a file a problem names; null is the process file. A path the explorer does not hold
   *  is left alone. */
  function openNamed(path: string | null) {
    if (path === null) openTab(processPath);
    else if (explorerFiles.some((file) => file.path === path)) openTab(path);
  }

  function commitNow() {
    commit.mutate(draftToSend(), {
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
        // A sent deletion is done either way: removed, or already gone from the branch.
        const gone = new Set((sent.deleted ?? []).map((file) => file.path));
        deletedFiles.update((current) =>
          Object.fromEntries(Object.entries(current).filter(([path]) => !gone.has(path))),
        );
        pristine.current = Object.fromEntries(
          Object.entries(pristine.current).filter(([path]) => path in kept),
        );
        setSelectedOverride(null);
        setExpandedOverride(null);
        setConfirmed(new Set());
        if (result.sync_error !== null) return;
        // The process on screen is gone, so there is nothing left here to edit. By the file of
        // the process the editor opened on as well as the text's own: a renamed text writes a
        // new file, and deleting the old one is how a process is renamed.
        const opened = processKey === null ? null : lanePath(configDir, processKey);
        if (gone.has(processPath) || (opened !== null && gone.has(opened))) heading?.onBack();
        // A failed sync leaves the lane on its old version, so the baseline stays too.
        else rebase(sent.content, result.sha);
      },
    });
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
  const editedCount = (dirty ? 1 : 0) + edited.size + repoEditCount + createdCount + deletedCount;

  /** A press of Commit: a commit that deletes a process asks first. */
  function requestCommit() {
    if (retiredProcesses.length > 0) setConfirmingDelete(true);
    else commitNow();
  }

  /** Stages each file the branch has for deletion, and drops each one made here. */
  function deletePaths(paths: string[]) {
    const nextCreated = { ...createdFiles.data };
    const nextDeleted = { ...deletedFiles.data };
    let nextEdits = repoEdits.data;
    // Text that lived only in this editor: a new file's body, and an edit held for a file now
    // staged for deletion. Restoring the file does not bring either back, so Undo does.
    const lostFiles: CreatedFiles = {};
    const lostEdits: RepoEdits = {};
    const staged: string[] = [];
    for (const path of paths) {
      const body = nextCreated[path];
      if (body !== undefined) {
        lostFiles[path] = body;
        delete nextCreated[path];
        closeTab(path);
        continue;
      }
      const repo = explorerFiles.find((file) => file.path === path)?.repo;
      if (!repo?.sha || repoLockReason(repo) !== undefined) continue;
      nextDeleted[path] = repo.sha;
      staged.push(path);
      const edit = nextEdits[path];
      if (edit) lostEdits[path] = edit;
      nextEdits = withoutEdit(nextEdits, path);
      // The process's own tab stays, to say what the commit does and offer the way back.
      if (path !== processPath) closeTab(path);
    }
    createdFiles.write(nextCreated);
    deletedFiles.write(nextDeleted);
    repoEdits.write(nextEdits);

    const files = Object.keys(lostFiles).length;
    const edits = Object.keys(lostEdits);
    if (files === 0 && edits.length === 0) return;
    const edited = edits.length === 1 ? fileNameOf(edits[0] ?? "") : `${edits.length} files`;
    showFlag({
      appearance: "info",
      title:
        files > 0
          ? `Deleted ${files} new ${files === 1 ? "file" : "files"}`
          : `Dropped your ${edits.length === 1 ? "edit" : "edits"} to ${edited}`,
      description:
        files > 0
          ? "The text was only in this editor."
          : "The next commit deletes the file. Undo keeps it and your edit.",
      actions: [
        {
          text: "Undo",
          onClick: () => {
            // Onto what is held now, so work done since the delete is kept.
            createdFiles.update((current) => ({ ...current, ...lostFiles }));
            repoEdits.update((current) => ({ ...current, ...lostEdits }));
            deletedFiles.update((current) =>
              Object.fromEntries(
                Object.entries(current).filter(([path]) => !staged.includes(path)),
              ),
            );
          },
        },
      ],
    });
  }

  function restorePaths(paths: string[]) {
    deletedFiles.update((current) =>
      Object.fromEntries(Object.entries(current).filter(([path]) => !paths.includes(path))),
    );
  }

  /** Stages a deletion again at the blob the branch holds now: the person has looked at what
   *  changed and still wants the file gone. */
  function keepDelete(path: string) {
    const repo = explorerFiles.find((file) => file.path === path)?.repo;
    if (!repo?.sha || repoLockReason(repo) !== undefined) return;
    deletedFiles.write({ ...deletedFiles.data, [path]: repo.sha });
  }

  function menuFor(node: TreeNode, at?: Point): MenuEntry[] {
    const inConfig = node.path === configDir || node.path.startsWith(`${configDir}/`);
    const start = node.path === configDir ? "" : `${node.path.slice(configDir.length + 1)}/`;
    const under = pathsUnder(node);
    const takeable = under.filter((path) => deletable.has(path) && !deletedSet.has(path));
    const staged = under.filter((path) => deletedSet.has(path));
    const within = (folder: string) => folder === node.path || folder.startsWith(`${node.path}/`);
    const empties = folders.filter(within);
    const place = () => at ?? below(document.activeElement);
    const adds = [
      ...constructEntries(node.path, at),
      ...(inConfig
        ? [
            {
              label: "File",
              icon: "file-yaml" as const,
              onClick: () => setMaking({ kind: "file", start, at: place() }),
            },
            {
              label: "Folder",
              icon: "folder" as const,
              onClick: () => setMaking({ kind: "folder", start, at: place() }),
            },
          ]
        : []),
    ];
    const entries: MenuEntry[] = adds.length > 0 ? [{ group: "Add new", items: adds }] : [];
    // The config folder itself is never one to delete: that would stage every file at once.
    if (node.path === configDir) return entries;
    if (takeable.length > 0 || empties.length > 0) {
      entries.push({
        group: "This folder",
        items: [
          {
            label: "Delete folder",
            icon: "trash",
            onClick: () => {
              deletePaths(takeable);
              setFolders((prev) => prev.filter((folder) => !within(folder)));
            },
          },
        ],
      });
    } else if (staged.length > 0) {
      entries.push({
        group: "This folder",
        items: [{ label: "Restore folder", icon: "undo", onClick: () => restorePaths(staged) }],
      });
    }
    return entries;
  }
  const openFolder = openPath.includes("/") ? openPath.slice(0, openPath.lastIndexOf("/")) : "";
  const startIn = openFolder.startsWith(`${configDir}/`)
    ? `${openFolder.slice(configDir.length + 1)}/`
    : "";

  function create(path: string) {
    setMaking(null);
    const parents = path.split("/").map((_, end, all) => all.slice(0, end + 1).join("/"));
    // Open down to it, and a new folder itself, so what was just made is in view.
    const opened = makingKind === "folder" ? parents : parents.slice(0, -1);
    setExpandedOverride((prev) =>
      opened.reduce((set, dir) => toggled(set, dir, true), prev ?? expanded),
    );
    if (makingKind === "folder") {
      setFolders((prev) => [...prev, path]);
      return;
    }
    setFolders((prev) => prev.filter((folder) => !path.startsWith(`${folder}/`)));
    createdFiles.write({ ...createdFiles.data, [path]: "" });
    openTab(path);
  }

  const knownPaths = explorerFiles.map((file) => file.path);
  const proposedPaths = previewFiles.map((file) => file.path);

  /** Every folder and file `construct` needs, named `name`, as new files filled from its shell. */
  function createConstruct(construct: Construct, name: string) {
    setMaking(null);
    const files = constructFiles(shells, construct, configDir, name);
    const [first, ...rest] = Object.keys(files);
    if (first === undefined) return;
    // A process runs on a Runtime, and the one on screen is the likeliest: the text says which.
    const body = (path: string) =>
      construct.kind === "process" ? withRuntimeLine(files[path] ?? "", runtime) : files[path];
    createdFiles.write({
      ...createdFiles.data,
      ...Object.fromEntries([first, ...rest].map((path) => [path, body(path) ?? ""])),
    });
    const parents = first.split("/").map((_, end, all) => all.slice(0, end + 1).join("/"));
    setExpandedOverride((prev) =>
      parents.slice(0, -1).reduce((set, dir) => toggled(set, dir, true), prev ?? expanded),
    );
    setFolders((prev) => prev.filter((folder) => !first.startsWith(`${folder}/`)));
    openTab(first);
  }

  /** An agent a step already names: the name is the config's, so a refusal is said in a flag. */
  function createNamedAgent(name: string) {
    const construct: Construct = { kind: "agent" };
    const problem = constructNameProblem(construct, name, configDir, knownPaths, proposedPaths);
    if (problem) showFlag({ appearance: "warning", title: problem });
    else createConstruct(construct, name);
  }

  /** Replaces a manifest that holds only comments with the agent shell, and adds whichever of
   *  the shell's other files nothing provides yet. */
  function fillFromShell(manifest: string, name: string) {
    const files = constructFiles(shells, { kind: "agent" }, configDir, name);
    const body = files[manifest];
    if (body === undefined) return;
    editRepoFile(manifest, body);
    const missing = Object.entries(files).filter(
      ([path]) => path !== manifest && !knownPaths.includes(path),
    );
    createdFiles.write({ ...createdFiles.data, ...Object.fromEntries(missing) });
  }

  const emptyManifest = AGENT_MANIFEST.exec(openPath);
  const canFillShell =
    !readOnly &&
    emptyManifest !== null &&
    Boolean(shells.agent) &&
    !(openPath in repoEdits.data) &&
    repoFile.data !== undefined &&
    isEmptyManifest(repoFile.data.content);

  // A referenced agent the branch, the catalog and this draft all lack: the commit would write
  // it a manifest that cannot run, so the way to a whole one is offered first.
  const missingAgents = (preview.data?.components ?? [])
    .filter(
      (component) =>
        component.kind === "agent" &&
        component.referenced &&
        !component.present_in_repo &&
        component.variants.length === 0,
    )
    .map((component) => component.name)
    .filter((name) => !(`${configDir}/agents/${name}/agent.yaml` in createdFiles.data));

  /** What a folder may be given, as the items under a menu's "Add new". `at` is where a
   *  right-click opened the menu; a menu opened from a button puts the focus back on that button
   *  before its item acts, so the prompt goes under it. */
  const constructEntries = (folder: string, at?: Point) =>
    constructsFor(folder, configDir).map((construct) => ({
      label: constructName(construct),
      icon: constructIcon(construct),
      unavailable: shells[construct.kind]
        ? undefined
        : "Factorial couldn't load its starting files. Reload the page.",
      onClick: () => setMaking({ construct, at: at ?? below(document.activeElement) }),
    }));

  return (
    // Two panes that scroll independently cannot live inside one scrolling column, and the
    // explorer has to stay put while a long file moves: the caller gives this a bounded height.
    <div className="flex h-full min-h-0 flex-col">
      <EditorActions
        readOnly={readOnly}
        heading={heading}
        commitButton={commitButton}
        onCommit={requestCommit}
        onValidate={() => check.mutate(draftToSend())}
        validating={check.isPending}
        unpermittedSlugs={needPermit}
        onPermitAndCommit={() =>
          permits.mutate(permitting(needPermit), { onSuccess: requestCommit })
        }
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
        requestError={commit.error ?? check.error ?? validate.error ?? raw.error ?? preview.error}
        verdict={verdict}
        deleting={Object.keys(deletedFiles.data).sort()}
        check={check.data}
        onOpenFile={openNamed}
        missingAgents={readOnly ? [] : missingAgents}
        onCreateAgent={createNamedAgent}
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
      <div ref={panes} className="flex min-h-0 flex-1 flex-col md:flex-row">
        <div
          ref={filePanel}
          style={{ "--explorer-width": `${paneWidth}px` } as CSSProperties}
          className="max-h-64 shrink-0 overflow-y-auto border-b border-default p-3 md:max-h-none md:w-(--explorer-width) md:max-w-[40%] md:border-b-0"
        >
          <div className="mb-2 flex items-center justify-between gap-2">
            <span className="text-heading-xxs text-subtle">Files</span>
            {!readOnly && (
              <span className="flex gap-1">
                <IconButton
                  icon="plus"
                  label="New file"
                  spacing="compact"
                  isSelected={makingKind === "file"}
                  onClick={(event) =>
                    setMaking({ kind: "file", start: null, at: below(event.currentTarget) })
                  }
                />
                <IconButton
                  icon="folder"
                  label="New folder"
                  spacing="compact"
                  isSelected={makingKind === "folder"}
                  onClick={(event) =>
                    setMaking({ kind: "folder", start: null, at: below(event.currentTarget) })
                  }
                />
                {/* The way to a construct without a folder to right-click, and from a keyboard. */}
                <DropdownMenu
                  trigger={{
                    label: "Add an agent, MCP server, process or skill",
                    icon: "more",
                    iconOnly: true,
                    appearance: "ghost",
                    spacing: "compact",
                  }}
                  items={[{ group: "Add new", items: constructEntries(configDir) }]}
                />
              </span>
            )}
          </div>
          {/* The name is asked for where the person pressed to add, not at the top of the panel,
              which may be a long way from the folder they were looking at. */}
          {making && "kind" in making && (
            <PopupAt
              at={making.at}
              label={making.kind === "file" ? "New file" : "New folder"}
              onClose={() => setMaking(null)}
            >
              <NewEntry
                key={`${making.kind}:${making.start ?? ""}`}
                kind={making.kind}
                configDir={configDir}
                start={making.start ?? startIn}
                problemOf={(path) =>
                  newPathProblem(path, making.kind, configDir, knownPaths, folders)
                }
                onCreate={create}
                onCancel={() => setMaking(null)}
              />
            </PopupAt>
          )}
          {making && "construct" in making && (
            <PopupAt
              at={making.at}
              label={constructLabel(making.construct)}
              onClose={() => setMaking(null)}
            >
              <NewEntry
                key={constructLabel(making.construct)}
                kind="file"
                configDir={configDir}
                start=""
                naming={{
                  label: `${constructLabel(making.construct)} in ${constructFolder(making.construct, configDir)}/`,
                  ...NAMING[making.construct.kind],
                }}
                problemOf={(name) =>
                  constructNameProblem(making.construct, name, configDir, knownPaths, proposedPaths)
                }
                onCreate={(name) => createConstruct(making.construct, name)}
                onCancel={() => setMaking(null)}
              />
            </PopupAt>
          )}
          {listedFrom && listing.isPending && (
            <div role="status" aria-label="Loading the files" className="space-y-2 py-1">
              {[60, 80, 70, 50].map((width) => (
                <Skeleton key={width} width={`${width}%`} />
              ))}
            </div>
          )}
          <ConfigTree
            files={explorerFiles}
            expanded={expanded}
            selected={selected}
            edited={edited}
            stale={staleSet}
            modified={modified}
            created={created}
            deleted={deletedSet}
            deletable={deletable}
            openPath={openPath}
            onToggle={readOnly ? undefined : toggleSelected}
            onExpand={toggleExpanded}
            folders={folders}
            onOpen={openTab}
            onDelete={readOnly ? undefined : deletePaths}
            onRestore={readOnly ? undefined : restorePaths}
            menuFor={readOnly ? undefined : menuFor}
            onContextMenu={readOnly ? undefined : (node, at) => setMenu({ node, at })}
          />
          <ErrorMessage error={listing.error} />
        </div>

        <PaneResizer
          width={paneWidth}
          min={MIN_WIDTH}
          max={clampWidth(Number.MAX_SAFE_INTEGER, panesWidth()) || paneWidth}
          left={() => filePanel.current?.getBoundingClientRect().left ?? 0}
          onResize={resizePane}
          onDone={() => writePaneWidth(keptWidth.current)}
        />
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
            ) : isProcess && loading ? (
              <div role="status" className="space-y-2 p-4">
                <VisuallyHidden>Loading the config</VisuallyHidden>
                {[72, 40, 56, 88, 64, 48, 80, 36].map((width) => (
                  <Skeleton key={width} width={`${width}%`} />
                ))}
              </div>
            ) : isProcess ? (
              // `process.yaml` is not a bundle file: `assemble.py` commits it from `yaml_text` and
              // never from the selection, so its text stays the editor's own rather than becoming a
              // proposal body that could diverge from the config that was parsed and version checked.
              <div className="flex h-full min-h-0 flex-col">
                {retiring && (
                  <SectionMessage
                    appearance="warning"
                    title="This process is deleted in the next commit"
                    actions={[{ text: "Restore", onClick: () => restorePaths([processPath]) }]}
                    className="m-3 shrink-0"
                  >
                    The sync retires it. Tickets already on it keep running.
                  </SectionMessage>
                )}
                <CodeEditor
                  label="process.yaml"
                  file={processPath}
                  value={text}
                  onChange={readOnly || retiring ? undefined : setText}
                  readOnly={readOnly || retiring}
                  placeholder={
                    readOnly
                      ? undefined
                      : "Start from a template, or paste an existing process file."
                  }
                  className="min-h-0 flex-1"
                />
              </div>
            ) : isCreated ? (
              <FilePane
                file={openExplorerFile}
                body={createdFiles.data[openPath] ?? ""}
                stale={false}
                isNew
                onChange={(body) => createdFiles.write({ ...createdFiles.data, [openPath]: body })}
                revertLabel="Delete this new file"
                onRevert={() => deletePaths([openPath])}
              />
            ) : openRepo ? (
              <FilePane
                file={openExplorerFile}
                body={repoEdits.data[openPath]?.body ?? repoFile.data?.content ?? ""}
                loading={
                  !(openPath in repoEdits.data) && Boolean(openRepo.sha) && repoFile.isPending
                }
                stale={staleSet.has(openPath)}
                // Staged for deletion: read as it is on the branch, with the way back. An
                // edit would be sent with the deletion, and the server refuses both for one path.
                deleted={openDeleted}
                lockReason={openDeleted ? "Deleted in the next commit." : openLock}
                revertLabel={openDeleted ? "Restore the file" : "Revert to the repository's copy"}
                onChange={
                  !readOnly && !openLock && !openDeleted && repoFile.data
                    ? (body) => editRepoFile(openPath, body)
                    : undefined
                }
                onRevert={
                  openDeleted
                    ? () => restorePaths([openPath])
                    : openPath in repoEdits.data
                      ? () => repoEdits.write(withoutEdit(repoEdits.data, openPath))
                      : undefined
                }
                onConfirm={
                  readOnly || openLock
                    ? undefined
                    : openDeleted
                      ? () => keepDelete(openPath)
                      : () => keepRepoEdit(openPath)
                }
                onFillShell={
                  canFillShell && !openLock && !openDeleted && emptyManifest?.[1]
                    ? () => fillFromShell(openPath, emptyManifest[1] ?? "")
                    : undefined
                }
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
      <ContextMenu
        at={menu?.at ?? null}
        label={menu ? `Actions for ${menu.node.name}` : ""}
        items={menu ? [...menuFor(menu.node, menu.at), ...assistantEntries(menu.node)] : []}
        onClose={() => setMenu(null)}
      />
      {confirmingDelete && (
        <ConfirmDialog
          title={
            retiredProcesses.length === 1
              ? `Delete the ${retiredProcesses[0]} process?`
              : `Delete ${retiredProcesses.length} processes?`
          }
          consequence={
            retiredProcesses.length === 1
              ? "The process is retired at the sync. Tickets already on it keep running. No new ticket can enter it. Config history can restore it."
              : `The sync retires ${retiredProcesses.join(", ")}. Tickets already on them keep running. No new ticket can enter them. Config history can restore them.`
          }
          confirmLabel="Commit and delete"
          appearance="danger"
          onConfirm={() => {
            setConfirmingDelete(false);
            commitNow();
          }}
          onCancel={() => setConfirmingDelete(false)}
        />
      )}
    </div>
  );
}
