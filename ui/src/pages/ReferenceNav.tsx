/** The reference pages' header controls: the links to the others and the theme switch
 *  (docs/design-system.md, Reference pages). */

import { LinkButton, SegmentedControl } from "../components/ui";
import { THEMES, useTheme } from "../theme";

export const REFERENCE_PAGES = [
  { name: "guide", label: "Guide" },
  { name: "catalogue", label: "Catalogue" },
  { name: "sample", label: "Sample" },
] as const;

export type ReferenceName = (typeof REFERENCE_PAGES)[number]["name"];

export function ReferenceNav({ current }: { current: ReferenceName }) {
  const { choice, setChoice } = useTheme();
  return (
    <span className="flex flex-wrap items-center gap-1">
      {REFERENCE_PAGES.filter((page) => page.name !== current).map((page) => (
        <LinkButton key={page.name} to={`/dev/${page.name}`} spacing="compact">
          {page.label}
        </LinkButton>
      ))}
      <span aria-hidden="true" className="mx-1 h-4 shrink-0 border-l border-default" />
      {/* A static file has no React to answer a press: `referenceHtml.tsx`'s script finds the
          switch by this attribute and does what `setChoice` would. */}
      <span data-theme-switch="">
        <SegmentedControl
          label="Theme"
          value={choice}
          onChange={setChoice}
          options={THEMES.map((theme) => ({ value: theme.id, label: theme.label }))}
        />
      </span>
    </span>
  );
}
