/**
 * What a person pointed at and what they attached, as chips: over the composer while it waits to
 * be sent, where each has a way out, and on the sent message afterwards. A file's chip is its
 * name, and nothing opens it: the bytes are not served back.
 *
 * A pointed-at chip is a symbol and a few of the thing's own
 * words; its card says what the assistant is told of it — the words that were there, the file and
 * lines of the config editor, the project — and gives the way back to it: its page, where the
 * thing is ringed when it can be found again, and the page it links to. The card is the
 * `HoverCard`, drawn on the page itself, so the frame the chip is in never cuts it off.
 */

import { Button, cx, HoverCard, Icon, IconButton } from "../ui";
import { useProjectName } from "./AskingAbout";
import { DashboardLink, useAssistant } from "./AssistantProvider";
import type { Pointed } from "./draft";
import { isDashboardPath } from "./paths";

/** What an item's payload holds, read back: only what has a label is a chip. */
export function pointedIn(payload: Record<string, unknown>): Pointed[] {
  const { context } = payload;
  if (!Array.isArray(context)) return [];
  return context.filter(
    (item): item is Pointed =>
      typeof item === "object" && item !== null && typeof (item as Pointed).label === "string",
  );
}

/** A place on this dashboard, or null: what a stored message holds is read, not trusted. */
const placeOn = (path: unknown) =>
  typeof path === "string" && isDashboardPath(path) ? path : null;

function Told({ item }: { item: Pointed }) {
  const { showWhere } = useAssistant();
  const page = placeOn(item.page);
  const linked = placeOn(item.href);
  const project = useProjectName(page);
  const [first, last] = item.lines ?? [];
  const text = typeof item.text === "string" ? item.text : "";
  return (
    <div className="max-w-80 space-y-1 p-3">
      <p className="font-semibold break-words text-default">{item.label}</p>
      {text && text !== item.label && (
        <p className="line-clamp-6 break-words whitespace-pre-wrap text-subtle">{text}</p>
      )}
      {item.file && (
        <p className="break-words text-subtlest">
          {item.file}
          {first !== undefined &&
            (first === last ? `, line ${first}` : `, lines ${first} to ${last}`)}
        </p>
      )}
      {project && <p className="text-subtlest">In {project}</p>}
      {(page || linked) && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 pt-1">
          {page && (
            <Button spacing="compact" iconBefore="eye" onClick={() => showWhere(item)}>
              Show where this was
            </Button>
          )}
          {linked && linked !== page && (
            <DashboardLink to={linked}>Open the linked page</DashboardLink>
          )}
        </div>
      )}
    </div>
  );
}

const CHIP =
  "flex h-7 max-w-full min-w-0 items-center rounded-md border border-default bg-surface text-body-sm";

export type FileChip = { name: string; isImage: boolean };

/** The files an item's payload lists, read back. */
export function filesIn(payload: Record<string, unknown>): FileChip[] {
  const { attachments } = payload;
  if (!Array.isArray(attachments)) return [];
  return attachments
    .filter(
      (file): file is { name: string; media_type?: unknown } =>
        typeof file === "object" && file !== null && typeof file.name === "string",
    )
    .map((file) => ({ name: file.name, isImage: String(file.media_type).startsWith("image/") }));
}

export function FileChips({
  files,
  onRemove,
}: {
  files: FileChip[];
  onRemove?: (at: number) => void;
}) {
  if (files.length === 0) return null;
  return (
    <ul aria-label="Attached files" className="flex flex-wrap gap-1">
      {files.map((file, at) => (
        <li
          // biome-ignore lint/suspicious/noArrayIndexKey: two files can share a name
          key={at}
          data-file-chip
          className={cx(CHIP, "gap-1.5 pl-2 text-default", !onRemove && "pr-2")}
        >
          <Icon glyph={file.isImage ? "image" : "file"} size="small" className="text-icon-subtle" />
          <span className="truncate">{file.name}</span>
          {onRemove && (
            <IconButton
              icon="close"
              label={`Remove ${file.name}`}
              spacing="compact"
              onClick={() => onRemove(at)}
            />
          )}
        </li>
      ))}
    </ul>
  );
}

export function PointedChips({
  pointed,
  onRemove,
}: {
  pointed: Pointed[];
  /** Absent on a sent message, which keeps its chips. */
  onRemove?: (at: number) => void;
}) {
  if (pointed.length === 0) return null;
  return (
    <ul aria-label="Pointed at" className="flex flex-wrap gap-1">
      {pointed.map((item, at) => (
        <li
          // biome-ignore lint/suspicious/noArrayIndexKey: the same thing can be pointed at twice
          key={at}
          data-pointed-chip
          className={CHIP}
        >
          {/* The trigger is as wide as this box at most, so its words cut off before they push
          the way out past the chip's edge. */}
          <span className="flex min-w-0 flex-1">
            <HoverCard
              label={`Pointed at ${item.label}`}
              icon="pointer"
              text={item.label}
              interactive
              placement="top-start"
            >
              <Told item={item} />
            </HoverCard>
          </span>
          {onRemove && (
            <IconButton
              icon="close"
              label={`Remove ${item.label}`}
              spacing="compact"
              onClick={() => onRemove(at)}
            />
          )}
        </li>
      ))}
    </ul>
  );
}
