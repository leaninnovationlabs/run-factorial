import { useEffect, useState } from "react";
import type { TimelineEntry } from "../../api/types";
import type { ForgeHost } from "../../forge";
import { formatAbsolute, formatElapsed, formatRelative } from "../../format";
import {
  type ConditionTone,
  ROLE_BOLD,
  ROLE_DOT,
  ROLE_ICON,
  ROLE_SUBTLE,
  ROLE_TEXT,
  TONE_ROLE,
} from "../../status";
import { cx, Icon, IconButton, InlineMessage, RelativeTime } from "../ui";
import {
  type EventFacts,
  type EventLink,
  factsFor,
  type Held,
  type LinkPulse,
  type LongContent,
  linkFor,
  linkPulse,
} from "./activity";

export function EventRow({
  entry,
  repo,
  forge,
  held,
  current = false,
  cancelled,
  onOpenText,
}: {
  entry: TimelineEntry;
  repo: string | null | undefined;
  forge?: ForgeHost;
  /** Non-null only on the block the ticket is stopped at, for the link's pulse. */
  held: Held | null;
  /** This row is still true of the ticket, so it may tell the reader what to do (`factsFor`). */
  current?: boolean;
  /** What overtook the attempt this row booked (`cancelledRetries`), which ends its countdown. */
  cancelled?: string;
  onOpenText: (content: LongContent) => void;
}) {
  const booked = factsFor(entry, current);
  const facts: EventFacts = cancelled
    ? { ...booked, primary: `Canceled: ${cancelled}`, dueAt: undefined, tone: "neutral" }
    : booked;
  const link = linkFor(entry, repo, forge);

  return (
    <li className="flex items-center gap-3 px-4 py-1.5 text-body">
      <ToneMark tone={facts.tone} />
      <span className={cx("shrink-0", facts.significant ? "text-default" : "text-subtle")}>
        {facts.label}
      </span>
      {/* Always rendered, empty or not: it is the flexible column that holds every row's link
          and timestamp at the same margin, and a row with no fact of its own to print (a bare
          rejection, say) would otherwise pull its link left to sit against the label. */}
      <span
        className={cx("min-w-0 flex-1 truncate text-body-sm", ROLE_TEXT[TONE_ROLE[facts.tone]])}
      >
        {facts.dueAt ? <RetryDue at={facts.dueAt} /> : facts.primary}
      </span>
      {link && <RowLink link={link} tone={facts.tone} pulse={linkPulse(held, facts.significant)} />}
      {facts.long && (
        <IconButton
          icon="maximize"
          label={`Read ${facts.long.title.toLowerCase()} in full`}
          spacing="compact"
          onClick={() => onOpenText({ ...(facts.long as LongContent), link: link ?? undefined })}
        />
      )}
      <span className="ml-auto shrink-0 text-body-sm text-subtlest tabular-nums">
        <RelativeTime iso={entry.created_at} />
      </span>
    </li>
  );
}

/**
 * How long a scheduled retry still has to wait.
 *
 * The only row on the page whose fact changes without a poll, so it keeps its own second hand
 * rather than waiting for one — a backoff is minutes long and a figure that jumps in ten-second
 * steps reads as broken rather than as counting.
 *
 * "about", never a bare "at": the worker recomputes the due time from the failed run's end and
 * acts on it on its next tick, so the attempt starts somewhere after this, not on it. Once the time
 * has passed the clock stops and the row goes back to reporting, like every other row.
 */
function RetryDue({ at }: { at: string }) {
  const due = new Date(at).getTime();
  const [now, setNow] = useState(() => Date.now());
  const waiting = now < due;

  useEffect(() => {
    if (!waiting) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [waiting]);

  return (
    <span className="inline-flex items-center gap-1">
      {waiting ? `in ${formatElapsed((due - now) / 1000)}` : `due ${formatRelative(at)}`}
      <InlineMessage iconLabel="When the retry runs" title="When the retry runs">
        Starts at about {formatAbsolute(at)}, possibly a minute later.
      </InlineMessage>
    </span>
  );
}

/**
 * The row's tone, drawn: an icon for the two tones a reader must notice — something failed, or a
 * person is needed — so they are told by shape as well as by colour, and a dot for the rest.
 */
function ToneMark({ tone }: { tone: ConditionTone }) {
  const role = TONE_ROLE[tone];
  if (role === "danger" || role === "warning") {
    const icon = ROLE_ICON[role];
    return (
      <Icon
        glyph={icon.glyph}
        size="small"
        label={role === "danger" ? "Failed" : "Needs attention"}
        className={icon.ink}
      />
    );
  }
  return (
    <span aria-hidden className={cx("mx-0.5 size-1.5 shrink-0 rounded-full", ROLE_DOT[role])} />
  );
}

/**
 * The row's one GitHub link, as a button.
 *
 * Colour and motion go together and mean one thing: this is what the ticket needs from you. The
 * row the ticket is stopped at takes its status colour and the graph's beat; every other row —
 * settled history, however red or green the event itself reads — takes the grey chip, so the page
 * has exactly one call to action on it. A branch name long enough to push the timestamp around
 * would cost the column its alignment, hence the cap and the truncation.
 */
function RowLink({
  link,
  tone,
  pulse,
}: {
  link: EventLink;
  tone: ConditionTone;
  pulse: LinkPulse;
}) {
  // The link to what stopped the ticket on a failure does not flash; it takes the bold danger role
  // and the error icon, which say "this is what failed" without an alarm.
  const failed = pulse === "error";
  const className = cx(
    "inline-flex max-w-[12rem] shrink-0 items-center gap-1 truncate rounded-sm px-1.5 py-0.5 text-body-sm",
    failed ? ROLE_BOLD.danger : ROLE_SUBTLE[TONE_ROLE[pulse === "none" ? "neutral" : tone]],
    pulse === "blink" && "flow-blink",
  );
  const mark = failed && <Icon glyph="status-error" size="small" label="Failed" />;

  // Same chip either way: an unaddressable name is still the name of the thing this row is about,
  // and demoting it out of the column would move every other row's timestamp. It just does not
  // pretend to go anywhere — an anchor to a ref that does not exist is worse than no anchor.
  if (link.href === null) {
    return (
      <span className="inline-flex shrink-0 items-center gap-1">
        <span className={className}>
          {mark}
          {link.label}
        </span>
        <InlineMessage iconLabel={`About ${link.label}`} title="Not a link yet">
          The agent had not chosen this name yet, so there is nothing on GitHub to open.
        </InlineMessage>
      </span>
    );
  }

  return (
    <a href={link.href} target="_blank" rel="noreferrer" className={className}>
      {mark}
      {link.label}
    </a>
  );
}
