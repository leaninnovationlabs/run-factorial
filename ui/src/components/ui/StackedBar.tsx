/**
 * One bar cut into parts: where a whole went, each part's share drawn as its length.
 *
 * The bar carries the shape; the row under it carries the facts — swatch, name and figure for
 * every part, including a part too small to see and a part worth nothing — so the figure reads
 * without a pointer, on a phone, and in a screen reader (the bar is one image whose name says
 * every part). The hover bubble only repeats a part's `tip`.
 *
 * Marks follow the house chart rules: 24px tall at most, 2px of surface between parts, the ends
 * rounded and nothing else, and no text in a series colour.
 */

import { type Look, lookStyle, Swatch, useDelegatedTip } from "./chart";
import { AnchoredTooltip } from "./Tooltip";

export type StackedSegment = {
  key: string;
  look: Look;
  label: string;
  /** The quantity, for the length; `text` is what a reader sees. */
  value: number;
  text: string;
  tip?: string;
};

export function StackedBar({ label, segments }: { label: string; segments: StackedSegment[] }) {
  const { tip, handlers } = useDelegatedTip();
  const drawn = segments.filter((segment) => segment.value > 0);
  return (
    <div>
      <div
        role="img"
        aria-label={`${label}: ${segments.map((s) => `${s.label} ${s.text}`).join(", ")}`}
        className="flex h-6 gap-0.5 overflow-hidden rounded-sm"
        {...handlers}
      >
        {drawn.length === 0 ? (
          <span className="flex-1 bg-neutral" />
        ) : (
          drawn.map((segment) => (
            <span
              key={segment.key}
              data-segment={segment.key}
              data-chart-look={segment.look}
              data-tip={segment.tip}
              style={{ flexGrow: segment.value, ...lookStyle(segment.look) }}
              className="min-w-0.5 basis-0 bg-[var(--look)]"
            />
          ))
        )}
      </div>
      <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-body-sm">
        {segments.map((segment) => (
          <li key={segment.key} className="inline-flex items-center gap-1.5">
            <Swatch look={segment.look} />
            <span className="text-subtle">{segment.label}</span>
            <span className="font-medium text-default tabular-nums">{segment.text}</span>
          </li>
        ))}
      </ul>
      {tip !== null && <AnchoredTooltip anchor={tip.anchor} content={tip.text} />}
    </div>
  );
}
