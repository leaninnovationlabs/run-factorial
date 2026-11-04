import { useViewParams } from "../../searchParams";
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

/** Active is the default and the URL's absence of `show`: paused projects are mostly toys, and
 *  the page a demo opens on should not need a click to hide them. */
export function useActiveView() {
  const [params, update] = useViewParams();
  const show = params.get("show");
  const value: ActiveView = show === "all" || show === "paused" ? show : "active";
  const setValue = (next: ActiveView) => update({ show: next === "active" ? null : next });
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
