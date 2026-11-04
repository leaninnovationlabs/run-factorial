import { formatAbsolute, formatRelative, NO_VALUE } from "../../format";
import { Tooltip } from "./Tooltip";

/**
 * "3 days ago", with the exact time a focus or hover away. The element takes focus so the tooltip
 * reaches a keyboard as well as a mouse; the exact time is also in `dateTime` for assistive
 * technology and copy-paste.
 */
export function RelativeTime({ iso }: { iso: string | null | undefined }) {
  if (!iso) return <span className="text-subtlest">{NO_VALUE}</span>;
  return (
    <Tooltip content={formatAbsolute(iso)} describes>
      {/* biome-ignore lint/a11y/noNoninteractiveTabindex: a tooltip's trigger has to be reachable by keyboard */}
      <time dateTime={iso} tabIndex={0} className="rounded-xs whitespace-nowrap">
        {formatRelative(iso)}
      </time>
    </Tooltip>
  );
}
