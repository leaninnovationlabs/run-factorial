/**
 * A run of tool calls as one row, so a reply that read ten things does not open with ten lines.
 * While a call runs the row names it, as the work in progress; once every call is done it counts
 * them and keeps the lines folded, since the answer below is what the reader came for. A failed
 * or a stopped call is counted on the folded row, even while a later one runs, so neither is
 * hidden behind the fold.
 *
 * Open or shut is the stream's to hold: a call that turns out to have drawn a card leaves the
 * run, which can move the row's key and mount it again.
 */

import type { ChatItem } from "../../../api/types";
import { Icon, Pressable } from "../../ui";
import { SpinnerGlyph } from "../../ui/SpinnerGlyph";
import { failed, ToolLine, target, words } from "./ToolLine";

/** `ToolLine`'s words name a call; the row at work says it is doing them. */
const DOING = new Map([
  ["Read", "Reading"],
  ["List", "Listing"],
  ["Validate", "Validating"],
  ["Draw", "Drawing"],
  ["Open", "Opening"],
  ["Propose", "Proposing"],
  ["Show", "Showing"],
]);

function doing(call: Record<string, unknown>): string {
  const said = words(call.name);
  const [verb = "", ...rest] = said.split(" ");
  const ing = DOING.get(verb);
  const phrase = ing ? [ing, ...rest].join(" ") : said;
  const about = target(call.input);
  return `${phrase}${about ? ` ${about}` : ""}…`;
}

export function ToolGroup({
  calls,
  open,
  onToggle,
}: {
  calls: ChatItem[];
  open: boolean;
  onToggle: () => void;
}) {
  const running = calls.find((call) => call.payload.status === "running");
  const failures = calls.filter((call) => failed(call.payload)).length;
  const stopped = calls.filter((call) => call.payload.status === "stopped").length;
  return (
    <div className="flex max-w-full flex-col gap-1.5 self-start">
      <Pressable
        aria-expanded={open}
        onClick={onToggle}
        className="flex max-w-full items-start gap-2 px-2 py-1 text-body-sm"
      >
        <span className="mt-0.5 shrink-0 text-subtle">
          {running ? (
            <SpinnerGlyph size="small" />
          ) : failures > 0 ? (
            <Icon glyph="status-error" size="small" className="text-icon-danger" />
          ) : (
            <Icon glyph={stopped > 0 ? "close" : "check"} size="small" />
          )}
        </span>
        <span className="min-w-0 flex-1 break-words text-subtle">
          {running
            ? doing(running.payload)
            : `Used ${calls.length} ${calls.length === 1 ? "tool" : "tools"}`}
          {failures > 0 && <span className="text-danger"> ({failures} failed)</span>}
          {stopped > 0 && ` (${stopped} stopped)`}
        </span>
        <Icon
          glyph={open ? "chevron-down" : "chevron-right"}
          size="small"
          className="mt-0.5 text-subtle"
        />
      </Pressable>
      {open && (
        <ul className="ml-2 flex flex-col gap-1 border-l border-default pl-3">
          {calls.map((call) => (
            <li key={call.id}>
              <ToolLine payload={call.payload} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
