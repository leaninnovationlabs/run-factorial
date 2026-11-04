/**
 * What is applied to the board, kept in the address: `?metric=cost&focus=cell:gate:12&sort=error-desc`.
 * A view can then be linked, and survives a reload. The ticket last clicked stays out of it — it
 * is the reader's place in the list, not part of the view.
 *
 * A parameter that does not parse is dropped rather than guessed at: a link to a lane that has
 * been renamed opens the board plain, not on the nearest match.
 */

import { useEffect, useMemo } from "react";
import { useViewParams } from "../../searchParams";
import type { Focus } from "./focus";
import type { LaneSort } from "./layout";
import { type BoardMode, useBoardMode } from "./ModeBar";
import { LANES, type Lane, METRICS, type Metric, metricOf, modeOf } from "./model";

const isLane = (value: string | undefined): value is Lane => LANES.includes(value as Lane);

function projectId(value: string | undefined): number | null {
  const id = Number(value);
  return Number.isInteger(id) && id > 0 ? id : null;
}

export function focusParam(focus: Focus | null): string | null {
  if (focus === null) return null;
  switch (focus.kind) {
    case "lane":
      return `lane:${focus.lane}`;
    case "project":
      return `project:${focus.projectId}`;
    default:
      return `cell:${focus.lane}:${focus.projectId}`;
  }
}

export function parseFocus(value: string | null): Focus | null {
  const [kind, first, second] = (value ?? "").split(":");
  if (kind === "lane" && isLane(first) && second === undefined) return { kind, lane: first };
  if (kind === "project" && second === undefined) {
    const id = projectId(first);
    return id === null ? null : { kind, projectId: id };
  }
  if (kind === "cell" && isLane(first)) {
    const id = projectId(second);
    return id === null ? null : { kind, lane: first, projectId: id };
  }
  return null;
}

export function sortParam(sort: LaneSort | null): string | null {
  return sort === null ? null : `${sort.lane}-${sort.dir}`;
}

export function parseSort(value: string | null): LaneSort | null {
  const [lane, dir] = (value ?? "").split("-");
  return isLane(lane) && (dir === "asc" || dir === "desc") ? { lane, dir } : null;
}

export function parseMetric(value: string | null): Metric | null {
  return METRICS.includes(value as Metric) ? (value as Metric) : null;
}

/**
 * The board's three applied controls, read from the address and written back to it. The metric
 * controller still owns the hover preview; what it PINS is written to the address the moment it
 * is pressed, and follows the address when something else changes it — the Overview link in the
 * navigation, say, which lands on the bare route.
 */
export function useBoardView() {
  const [params, update] = useViewParams();
  const focusRaw = params.get("focus");
  const sortRaw = params.get("sort");
  const focus = useMemo(() => parseFocus(focusRaw), [focusRaw]);
  const laneSort = useMemo(() => parseSort(sortRaw), [sortRaw]);
  const pinned = parseMetric(params.get("metric"));
  const mode = useBoardMode(modeOf(pinned));
  const { controller } = mode;

  // Only when the address changes. A press writes the address itself, so by the time this runs
  // the two agree and it does nothing; it is there for the address changing under the page.
  useEffect(() => {
    if (metricOf(controller.getState().locked) !== pinned) {
      controller.lock(modeOf(pinned));
    }
  }, [pinned, controller]);

  const writeMetric = () => update({ metric: metricOf(mode.controller.getState().locked) });

  const view: BoardMode = {
    ...mode,
    controller: {
      ...mode.controller,
      select: (next) => {
        mode.controller.select(next);
        writeMetric();
      },
      clear: () => {
        mode.controller.clear();
        writeMetric();
      },
    },
  };

  return {
    view,
    focus,
    laneSort,
    setFocus: (next: Focus | null) => update({ focus: focusParam(next) }),
    setLaneSort: (next: LaneSort | null) => update({ sort: sortParam(next) }),
    reset: () => {
      mode.controller.clear();
      update({ metric: null, focus: null, sort: null });
    },
  };
}
