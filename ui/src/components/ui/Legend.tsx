/**
 * What each colour in a figure means: a swatch and a word per series. Drawn for two series or
 * more and for one series not at all — one colour needs no key, the figure's title is the key.
 */

import { type Look, Swatch } from "./chart";

export type LegendItem = { key: string; look: Look; label: string };

export function Legend({ label, items }: { label: string; items: LegendItem[] }) {
  if (items.length < 2) return null;
  return (
    <ul aria-label={label} className="flex flex-wrap gap-x-4 gap-y-1 text-body-sm text-subtle">
      {items.map((item) => (
        <li key={item.key} className="inline-flex items-center gap-1.5">
          <Swatch look={item.look} />
          {item.label}
        </li>
      ))}
    </ul>
  );
}
