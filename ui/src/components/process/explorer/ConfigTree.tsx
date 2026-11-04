/**
 * The `.factorial/` explorer: the left pane of the config drawer, purely presentational.
 * `expanded`, the selection sets and the open path all arrive as props and every change goes out
 * through a callback, which is what makes this renderable with `renderToString` — folding is
 * exercised by rendering twice with different props, never by a click.
 *
 * It draws two overlapping populations at once, and the badges are how they stay distinguishable:
 * a file the branch HAS is edited in place and drawn as modified, a file the commit WOULD
 * WRITE is tickable, and a manifest about to become a comments-only stub is called out before the
 * commit exists. That last one is decided IN here rather than handed down:
 * `.factorial/agents/<name>/agent.yaml` unticked is exactly the file `config/scaffold/assemble.py`
 * replaces with its stub template — never derived from a variant's `offered`/`default_on`, which
 * says what the catalog COULD write, not what this selection will.
 *
 * The retired `agents` readiness row lived as one paragraph of prose about every agent at once.
 * Its verdict is now `ConfigFile.warning`, drawn on the manifest it is about.
 */

import { Button, Checkbox, cx, Icon, IconButton, InlineMessage } from "../../ui";
import { repoLockReason } from "./repoEdits";
import {
  buildTree,
  type ChangeKind,
  changeOf,
  EMPTY,
  type ExplorerFile,
  isSelectable,
  selectableUnder,
  type TreeNode,
  tickOf,
} from "./tree";

export interface ConfigTreeProps {
  files: ExplorerFile[];
  expanded: Set<string>;
  selected: Set<string>;
  edited: Set<string>;
  stale: Set<string>;
  /** Repo files the commit would rewrite. Empty in the read-only drawer, where no
   *  commit is coming and every row is just what the commit holds. */
  modified?: Set<string>;
  /** Files the commit creates that no tick governs: a new lane's own process file. */
  created?: Set<string>;
  /** Folders made in the editor that hold no file yet. */
  folders?: string[];
  /** The path whose body the right pane is showing. */
  openPath: string | null;
  /** Absent in the read-only drawer, where nothing is tickable and no commit is coming. */
  onToggle?: (paths: string[], on: boolean) => void;
  onExpand: (path: string, open: boolean) => void;
  onOpen: (path: string) => void;
}

function isAgentManifest(file: ExplorerFile): boolean {
  return /\/agents\/[^/]+\/agent\.yaml$/.test(file.path);
}

function fileIcon(name: string, change: ChangeKind | null) {
  return (
    <Icon
      glyph={name.endsWith(".md") ? "file-markdown" : "file-yaml"}
      className={change ? CHANGE_INK[change] : "text-icon-subtle"}
    />
  );
}

export function ConfigTree({
  files,
  expanded,
  selected,
  edited,
  stale,
  modified = EMPTY,
  created = EMPTY,
  folders = [],
  openPath,
  onToggle,
  onExpand,
  onOpen,
}: ConfigTreeProps) {
  const roots = buildTree(files, folders);
  return (
    <div className="text-body">
      <ul>
        {roots.map((node) => (
          <TreeRow
            key={node.path}
            node={node}
            depth={0}
            expanded={expanded}
            selected={selected}
            edited={edited}
            stale={stale}
            modified={modified}
            created={created}
            openPath={openPath}
            onToggle={onToggle}
            onExpand={onExpand}
            onOpen={onOpen}
          />
        ))}
      </ul>
    </div>
  );
}

/** What the commit will do to a row, in ink: success creates, information changes
 *  something already there — the two outcomes worth telling apart at a glance before pressing the
 *  button. Neither is warning: the warning ink is for the unusable-manifest and drift marks, which
 *  are problems to fix, and a file the operator deliberately edited is not one. */
const CHANGE_INK: Record<ChangeKind, string> = {
  added: "text-success",
  modified: "text-information",
};

/** The row the right pane is showing: the finesse selected ground, a bar, and medium weight —
 *  never the ground alone. */
const OPEN_ROW =
  "bg-selected before:absolute before:inset-y-1 before:left-0 before:w-0.5 before:rounded-full before:bg-neutral-bold before:content-['']";

interface RowProps {
  node: TreeNode;
  depth: number;
  expanded: Set<string>;
  selected: Set<string>;
  edited: Set<string>;
  stale: Set<string>;
  modified: Set<string>;
  created: Set<string>;
  openPath: string | null;
  onToggle?: (paths: string[], on: boolean) => void;
  onExpand: (path: string, open: boolean) => void;
  onOpen: (path: string) => void;
}

function TreeRow(props: RowProps) {
  return props.node.kind === "dir" ? <DirRow {...props} /> : <FileRow {...props} />;
}

function DirRow(props: RowProps) {
  const { node, depth, expanded, selected, modified, created, onToggle, onExpand } = props;
  const isOpen = expanded.has(node.path);
  const tick = tickOf(node, selected);
  const selectable = selectableUnder(node);
  const selectedCount = selectable.filter((path) => selected.has(path)).length;
  const change = changeOf(node, selected, modified, created);
  return (
    <li>
      <div
        style={{ paddingLeft: depth * 14 }}
        className="flex items-center gap-1.5 rounded-sm py-0.5 pr-1 hover:bg-neutral-subtle-hovered"
      >
        <IconButton
          icon={isOpen ? "chevron-down" : "chevron-right"}
          label={isOpen ? `Collapse ${node.name}` : `Expand ${node.name}`}
          aria-expanded={isOpen}
          spacing="compact"
          onClick={() => onExpand(node.path, !isOpen)}
        />
        {onToggle && selectable.length > 0 && (
          <Checkbox
            label={`Include every file in ${node.name}`}
            labelHidden
            isChecked={tick === "on"}
            isIndeterminate={tick === "partial"}
            onChange={() => onToggle(selectable, tick !== "on")}
          />
        )}
        <Icon
          glyph={isOpen ? "folder-open" : "folder"}
          className={change ? CHANGE_INK[change] : "text-icon-subtle"}
        />
        <span className={cx("truncate font-medium", change ? CHANGE_INK[change] : "text-default")}>
          {node.name}
        </span>
        {selectable.length > 0 && (
          <span className="ml-auto shrink-0 text-body-sm text-subtlest tabular-nums">
            {selectedCount}/{selectable.length}
          </span>
        )}
      </div>
      {isOpen && (
        <ul>
          {node.children.map((child) => (
            <TreeRow {...props} key={child.path} node={child} depth={depth + 1} />
          ))}
        </ul>
      )}
    </li>
  );
}

function FileRow(props: RowProps) {
  const { node, depth, selected, edited, stale, modified, created, openPath, onToggle, onOpen } =
    props;
  const file = node.file;
  if (!file) return null;
  const selectable = isSelectable(file);
  const inRepo = Boolean(file.repo) || Boolean(file.proposal?.present_in_repo);
  // The editor edits a repo file in place unless the pin and the head disagree on it.
  const locked = !onToggle || (file.repo !== undefined && repoLockReason(file.repo) !== undefined);
  const isSelected = selected.has(node.path);
  const isStub = selectable && isAgentManifest(file) && !isSelected;
  const warning = file.repo?.warning ?? null;
  const drift = file.repo?.drift;
  const change = changeOf(node, selected, modified, created);
  return (
    <li>
      <div
        style={{ paddingLeft: depth * 14 + 28 }}
        className={cx(
          "relative flex items-center gap-1.5 rounded-sm py-0.5 pr-1 hover:bg-neutral-subtle-hovered",
          openPath === node.path && OPEN_ROW,
          isStub && "border-l-2 border-dashed border-bold",
        )}
      >
        {onToggle &&
          (selectable ? (
            <Checkbox
              label={`Include ${node.name}`}
              labelHidden
              isChecked={isSelected}
              onChange={() => onToggle([node.path], !isSelected)}
            />
          ) : (
            <span className="w-3.5 shrink-0" />
          ))}
        {fileIcon(node.name, change)}
        <Button
          appearance="subtle"
          spacing="compact"
          aria-current={openPath === node.path ? "true" : undefined}
          onClick={() => onOpen(node.path)}
          className="min-w-0 justify-start"
        >
          <span
            className={cx(
              "truncate",
              openPath === node.path && "font-medium",
              change ? CHANGE_INK[change] : inRepo && !warning ? "text-subtle" : "text-default",
            )}
          >
            {node.name}
          </span>
        </Button>
        {warning && (
          <InlineMessage appearance="warning" iconLabel={`Unusable: ${node.name}`} title="Unusable">
            {warning}
          </InlineMessage>
        )}
        {edited.has(node.path) && (
          <span className="shrink-0 text-body-sm text-subtlest">Edited</span>
        )}
        {stale.has(node.path) && (
          <Button appearance="warning" spacing="compact" onClick={() => onOpen(node.path)}>
            Review
          </Button>
        )}
        {isStub && <span className="shrink-0 text-body-sm text-subtlest">Empty template</span>}
        {drift && drift !== "same" && drift !== "unknown" && (
          <InlineMessage
            appearance="warning"
            iconLabel={`${node.name}: ${DRIFT_LABELS[drift]}`}
            secondaryText={DRIFT_LABELS[drift]}
          >
            {DRIFT_HINTS[drift]}
          </InlineMessage>
        )}
        {inRepo && !file.proposal && (
          <span className="flex shrink-0 items-center gap-1 text-body-sm text-subtlest">
            {locked && <Icon glyph="lock" size="small" />} In repo
          </span>
        )}
      </div>
    </li>
  );
}

/** Only the three that are worth a mark. `same` is the expected case and says nothing; `unknown`
 *  means the head could not be read, which is a fact about GitHub rather than about this file. */
const DRIFT_LABELS: Record<string, string> = {
  differs: "Changed on branch",
  added: "Not in this version",
  removed: "Deleted on branch",
};

const DRIFT_HINTS: Record<string, string> = {
  differs: "The factorial branch has a different version of this file. Sync config to pick it up.",
  added: "Added to the repository since this version was synced. Sync config to pick it up.",
  removed: "Deleted from the repository since this version was synced. This version still uses it.",
};
