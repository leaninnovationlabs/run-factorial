import { cx, Icon, IconButton, Pressable, Tooltip } from "../ui";

export type FileTab = {
  key: string;
  /** The full path, for the hover: the tab shows the name and its folder. */
  path: string;
  /** Edited and not yet committed. */
  modified: boolean;
};

/**
 * The files open in the editor, one tab each, over the file on show. Closing a tab only puts the
 * file away: its edits stay held, and the next Commit and sync carries them with the rest.
 */
export function FileTabs({
  tabs,
  active,
  onSelect,
  onClose,
}: {
  tabs: FileTab[];
  active: string | null;
  onSelect: (key: string) => void;
  onClose: (key: string) => void;
}) {
  return (
    <div
      role="tablist"
      aria-label="Open files"
      className="flex shrink-0 overflow-x-auto border-b border-default bg-surface-sunken"
    >
      {tabs.map((tab) => {
        const isActive = tab.key === active;
        const parts = tab.path.split("/");
        const name = parts.at(-1) ?? tab.path;
        const folder = parts.at(-2);
        return (
          <div
            key={tab.key}
            className={cx(
              "relative flex shrink-0 items-center border-r border-default transition-colors",
              // The open file carries a bar along its top, as a tab carries one along its foot.
              "before:absolute before:inset-x-0 before:top-0 before:border-t-2 before:border-selected before:transition before:duration-200 before:ease-out-bold motion-reduce:before:transition-none",
              isActive
                ? "bg-surface before:scale-x-100"
                : "before:scale-x-0 hover:bg-neutral-subtle-hovered",
            )}
          >
            <Tooltip content={tab.path}>
              <Pressable
                appearance="subtle"
                role="tab"
                aria-selected={isActive}
                tabIndex={isActive ? 0 : -1}
                onClick={() => onSelect(tab.key)}
                className={cx(
                  "focus-inset flex items-center gap-1.5 py-2 pr-1 pl-3 text-body-sm whitespace-nowrap",
                  !isActive && "text-subtle",
                )}
              >
                <Icon glyph={name.endsWith(".md") ? "file-markdown" : "file-yaml"} size="small" />
                <span className="font-medium">{name}</span>
                {folder && folder !== ".factorial" && (
                  <span className="text-subtlest">{folder}</span>
                )}
                {tab.modified && (
                  <span
                    role="img"
                    aria-label="Edited"
                    className="size-1.5 rounded-full bg-warning-bold"
                  />
                )}
              </Pressable>
            </Tooltip>
            <IconButton
              icon="close"
              label={`Close ${name}`}
              spacing="compact"
              onClick={() => onClose(tab.key)}
              className="mr-1"
            />
          </div>
        );
      })}
    </div>
  );
}
