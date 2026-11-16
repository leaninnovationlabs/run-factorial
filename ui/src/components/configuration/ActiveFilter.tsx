import { useState } from "react";
import { useViewParams } from "../../searchParams";
import { safeGet, safeSet } from "../../storage";
import { SegmentedControl } from "../ui";

export type ActiveView = "all" | "active" | "paused";

export function shows(view: ActiveView, active: boolean): boolean {
  switch (view) {
    case "all":
      return true;
    case "active":
      return active;
    case "paused":
      return !active;
  }
}

const asView = (value: string | null): ActiveView | null =>
  value === "all" || value === "active" || value === "paused" ? value : null;

/**
 * Which projects a list shows, kept in the URL's `show`.
 *
 * Without `remember` — the Overview, the page a demo opens on — Active is the default and the
 * absence of `show`: paused projects are mostly toys, and hiding them should not need a click.
 *
 * With `remember`, a browser storage key — the Projects list, where a reader goes to find a
 * paused one too — All is the default, and the reader's last press is kept and applies wherever
 * the address has no `show`, so coming back finds the list as they left it. A `show` in a link
 * that was followed is honoured but not kept: only a press is the reader's choice.
 */
export function useActiveView(remember?: string) {
  const [params, update] = useViewParams();
  const [kept, setKept] = useState(() => (remember ? asView(safeGet(remember)) : null));
  const fallback: ActiveView = remember ? (kept ?? "all") : "active";
  const value = asView(params.get("show")) ?? fallback;
  const setValue = (next: ActiveView) => {
    if (remember) {
      safeSet(remember, next);
      setKept(next);
    }
    const omitted: ActiveView = remember ? "all" : "active";
    update({ show: next === omitted ? null : next });
  };
  return [value, setValue] as const;
}

export function ActiveFilter({
  value,
  onChange,
  projects,
}: {
  value: ActiveView;
  onChange: (next: ActiveView) => void;
  projects: readonly { active: boolean }[];
}) {
  const active = projects.filter((project) => project.active).length;
  return (
    <SegmentedControl
      label="Filter projects by admission"
      value={value}
      onChange={onChange}
      options={[
        { value: "all", label: "All", count: projects.length },
        { value: "active", label: "Active", count: active },
        { value: "paused", label: "Paused", count: projects.length - active },
      ]}
    />
  );
}
