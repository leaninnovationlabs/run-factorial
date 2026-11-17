/**
 * The issue list: every ticket the current focus selects — a lane across all projects, a project
 * across all lanes, or the one cell where the two meet.
 *
 * The board answers "where should I look"; this answers "which issues, and let me open one". It
 * sits beside the board rather than below it, which is what decided its shape: a column roughly
 * twenty rems wide cannot carry the five columns a full-width table did, and a reader scanning
 * beside the board needs rows they can take in at a glance rather than a grid to read across.
 *
 * So each ticket is ONE line, and the two columns that were dropped are still on it:
 *
 *   - LANE became the swatch, painted through `appearanceOf` — the same function that decides
 *     what that ticket's square looks like on the board. Which is better than the word was: the
 *     eye matches a colour to the square it just came from without reading anything, and when a
 *     metric is shading the board the swatch follows it onto the heat ramp rather than going on
 *     insisting about a lane nobody is currently colouring by.
 *   - PROJECT became the dim suffix on the right, and only when the focus does not already fix
 *     it. Under a project or cell focus every row would carry the same word, which is a column
 *     of noise taking width from the rest of the row.
 *
 * With a metric shading the board a third field joins them: that ticket's figure for the metric,
 * between the number and the project. It comes and goes with the shading, so the row is only ever
 * as wide as the question being asked.
 *
 * Step is the one thing that left the row face, and it is on the row link's tooltip with the lane
 * name. In a column this narrow it was the field least often the reason anybody opened the list.
 *
 * With nothing focused it lists the whole board rather than an invitation to click. The list is
 * the readable form of what the board draws — a reader who wants a name, a number or a link is
 * here, not squinting at a 7px square — so it has something to say before any selection is made,
 * and focusing only ever narrows it.
 *
 * Which is why it searches, sorts and pages: holding the whole factory by default, a flat column
 * of three hundred rows is a worse answer than no column at all. They compose in that order — the
 * search narrows, the sort ranks, the paging cuts — so a query never changes the ordering.
 *
 * The paging is measured (`useRowCapacity`), so a page is exactly as many rows as there is room
 * for and NOTHING scrolls: the page's fill layout decides the room, the list fills it. One pager
 * turns it, the same one every table uses.
 *
 * On a phone it stands alone: the Overview draws no board there, the rows grow to a thumb's
 * height (`ROW_PX_TOUCH`), and the line under the heading stops pointing at a board that is not
 * on the page.
 */

import { useId, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { NO_VALUE } from "../../format";
import { useRowLink } from "../rowLink";
import {
  Badge,
  cx,
  Icon,
  IconButton,
  LinkButton,
  Lozenge,
  Pagination,
  Select,
  TextField,
  Tooltip,
} from "../ui";
import { useIsPhone } from "../ui/media";
import { describeFocus, type Focus } from "./focus";
import {
  defaultSort,
  issueNumber,
  naturalDir,
  pageOf,
  pageOfNode,
  ROW_PX,
  ROW_PX_TOUCH,
  rowFacts,
  type Sort,
  type SortKey,
  searchIssues,
  sortIssues,
  useRowCapacity,
} from "./list";
import {
  appearanceOf,
  DEFAULT_THRESHOLDS,
  formatMetric,
  type IssueNode,
  LANE_HINTS,
  LANE_LABELS,
  laneOf,
  METRIC_SPECS,
  type Metric,
  type Mode,
  metricOf,
  modeOf,
  type ProjectBand,
  type Thresholds,
} from "./model";
import { focusParam } from "./view";

/**
 * What the list can be sorted by — one entry per column the reader can actually see.
 *
 * Split into a key and a direction rather than ten fixed options, because the direction is a
 * property of the key: choosing "Title" and then having to fix the arrow is a press that says
 * nothing. `naturalDir` supplies the interesting end and the toggle beside it overrides.
 *
 * Two keys are conditional, for the same reason: a column the list is not drawing is not
 * something to offer to sort by.
 */
const SORT_KEYS: { key: SortKey; label: string; needs?: "metric" | "project" }[] = [
  { key: "status", label: "Status" },
  { key: "number", label: "Issue" },
  { key: "project", label: "Project", needs: "project" },
  { key: "metric", label: "Severity", needs: "metric" },
];

/**
 * The metric's own noun, as a sort option — "Cost", not "Severity".
 *
 * The option is only ever offered while a metric is shading the board, and by then the reader has
 * picked one by name on the rail beside it. "Severity" made them work out which of the four they
 * would be ordering by; the noun says it.
 */
function metricLabel(metric: Metric): string {
  const noun = METRIC_SPECS[metric].noun;
  return noun.charAt(0).toUpperCase() + noun.slice(1);
}

export interface IssueListProps {
  /** What the reader has clicked into, or null — in which case this lists the whole board. */
  focus: Focus | null;
  /** Already filtered to the focus. */
  nodes: IssueNode[];
  projects: ProjectBand[];
  /** The mode the board is drawing, so a row's swatch agrees with its square. */
  mode: Mode;
  thresholds?: Thresholds;
  /** The ticket whose square was last clicked on the board, if any. */
  selected?: number | null;
}

export function IssueList({
  focus,
  nodes,
  projects,
  mode,
  thresholds = DEFAULT_THRESHOLDS,
  selected = null,
}: IssueListProps) {
  // Memoised on the array it is derived from, not rebuilt per render: the sort below closes over
  // it, so a fresh map every render would be a fresh sort every render.
  const names = useMemo(
    () => new Map(projects.map((project) => [project.id, project.name])),
    [projects],
  );
  const metric = metricOf(mode);
  const showProject = focus === null || focus.kind === "lane";

  const headingId = useId();
  const [query, setQuery] = useState("");
  const [chosen, setChosen] = useState<Sort | null>(null);
  const [page, setPage] = useState(0);
  const touch = useIsPhone();
  const body = useRowCapacity<HTMLDivElement>(touch ? ROW_PX_TOUCH : ROW_PX);

  // Null means "whichever the metric implies", so turning a metric on re-ranks the list without
  // the reader having to ask — and an explicit choice survives them turning it off again.
  //
  // Except when that choice names a column the list has stopped drawing: a focus can take the
  // project column away and a metric can be cleared, and ordering by an invisible field looks
  // like no order at all. `sortIssues` would still do something sensible; the reader would not
  // know what.
  const picked = chosen ?? defaultSort(metric);
  const orphaned =
    (picked.key === "project" && !showProject) || (picked.key === "metric" && metric === null);
  const sort = orphaned ? defaultSort(metric) : picked;

  const shown = useMemo(
    () => sortIssues(searchIssues(nodes, query), sort, metric, (id) => names.get(id) ?? String(id)),
    [nodes, query, sort, metric, names],
  );
  const view = pageOf(shown, page, body.rows);

  // Back to the first page whenever the QUESTION changes, rather than whenever the answer does.
  // `pageOf` already clamps a page past the end, so this is only about not stranding a reader on
  // page nine of a search they just started. Adjusting during render is the documented way; an
  // effect would paint the wrong page first.
  const question = `${query}|${sort.key}:${sort.dir}|${focusParam(focus) ?? "-"}`;
  const [asked, setAsked] = useState(question);
  if (asked !== question) {
    setAsked(question);
    setPage(0);
  }

  // A square clicked on the board turns to the page its ticket is on. AFTER the reset above and
  // not before it: clicking a square changes the focus as well, so both adjustments run in the
  // same render, and the one that knows where the reader is going has to be the one that lands.
  //
  // Starting at null rather than at `selected` so a list that mounts with a selection already
  // made turns to it, rather than showing page one of a list it was told to open partway down.
  const [shownFor, setShownFor] = useState<number | null>(null);
  if (shownFor !== selected) {
    setShownFor(selected);
    const at = selected === null ? null : pageOfNode(shown, selected, body.rows);
    if (at !== null) setPage(at);
  }

  const project = focusedProject(focus);
  const sortOptions = SORT_KEYS.filter(
    (option) =>
      option.needs === undefined || (option.needs === "metric" ? metric !== null : showProject),
  ).map((option) => ({
    value: option.key,
    label: option.needs === "metric" && metric !== null ? metricLabel(metric) : option.label,
  }));

  return (
    <section
      aria-labelledby={headingId}
      className="factory-board__panel factory-board__list rounded-lg border border-default bg-surface"
    >
      <header className="flex items-start justify-between gap-4 border-b border-default px-4 py-3">
        <div className="min-w-0 space-y-0.5">
          <h2 id={headingId} className="flex items-center gap-2 text-heading-xs text-default">
            <span className="truncate">
              {focus === null ? "Tickets" : describeFocus(focus, projects)}
            </span>
            <Badge max={999}>{view.total}</Badge>
          </h2>
          {/* Coloured whenever either control is on, in step with the same line over the board:
              the two panels are one view, and what narrowed this list is what shaded that one. */}
          <p
            className="factory-board__note text-body-sm"
            data-on={focus !== null || metric !== null || undefined}
          >
            {focus !== null
              ? subtitle(focus)
              : touch
                ? "Every ticket in every project."
                : "Every ticket. Select part of the board to narrow the list."}
          </p>
        </div>
        {/* The way OUT of the board, for a focus that has narrowed to one project. A lane focus
            spans every project and has no single page to offer, so it gets nothing. */}
        {project !== null && (
          <LinkButton to={`/projects/${project}`} spacing="compact">
            View project
          </LinkButton>
        )}
      </header>

      <div className="flex items-center gap-2 border-b border-default px-3 py-2">
        <TextField
          type="search"
          aria-label="Search tickets"
          // What to type, short enough to fit beside the sort on a phone; the name says it searches.
          placeholder="Number or title"
          spacing="compact"
          elemBeforeInput={<Icon glyph="search" size="small" />}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <Select
          aria-label="Sort tickets by"
          spacing="compact"
          width="small"
          options={sortOptions}
          value={sort.key}
          onChange={(event) => {
            const key = event.target.value as SortKey;
            setChosen({ key, dir: naturalDir(key) });
          }}
        />
        <IconButton
          icon={sort.dir === "asc" ? "sort-up" : "sort-down"}
          label={sort.dir === "asc" ? "Sort descending" : "Sort ascending"}
          appearance="default"
          spacing="compact"
          onClick={() => setChosen({ key: sort.key, dir: sort.dir === "asc" ? "desc" : "asc" })}
        />
      </div>

      {/* The measured box, and it is a wrapper rather than the `<ul>` itself so that it stays
          mounted when there is nothing to list: `useRowCapacity` attaches its observer once, and
          a box that came and went with the rows would leave that observer watching a dead node
          and the page size frozen at whatever it last saw. */}
      <div ref={body.ref} className="factory-board__list-body">
        {view.total === 0 ? (
          <p className="px-4 py-5 text-body-sm text-subtle">
            {query.trim() === "" ? "No tickets here." : `Nothing matches "${query}".`}
          </p>
        ) : (
          <ul className="factory-board__list-rows">
            {view.rows.map((node) => (
              <IssueRow
                key={node.id}
                node={node}
                project={showProject ? (names.get(node.projectId) ?? NO_VALUE) : null}
                metric={metric}
                thresholds={thresholds}
                selected={node.id === selected}
              />
            ))}
          </ul>
        )}
      </div>

      {view.total > 0 && (
        <footer className="flex min-h-10 items-center justify-between gap-2 border-t border-default px-3 py-1 text-body-sm text-subtle tabular-nums">
          <span>
            {view.from} to {view.to} of {view.total}
          </span>
          <Pagination
            pages={view.pages}
            selectedIndex={view.page}
            onChange={setPage}
            label="Ticket pages"
          />
        </footer>
      )}
    </section>
  );
}

/**
 * One line.
 *
 * A real `<Link>` inside it plus `useRowLink` for the mouse convenience, exactly as `TicketTable`
 * does it — so the row is one keyboard stop and "open in new tab" still works. The link carries
 * the row's facts on its tooltip, which a keyboard reaches as well as a pointer.
 */
function IssueRow({
  node,
  project,
  metric,
  thresholds,
  selected,
}: {
  node: IssueNode;
  /** null when the focus already fixes the project. */
  project: string | null;
  metric: Metric | null;
  thresholds: Thresholds;
  /** This is the square that was clicked on the board. */
  selected: boolean;
}) {
  const lane = laneOf(node);
  const rowLink = useRowLink(`/tickets/${node.id}`);
  // `appearanceOf` rather than the lane, so the swatch and the square are one decision.
  const look = appearanceOf(node, modeOf(metric), thresholds);
  const spend = metric === null ? "" : formatMetric(node, metric);
  return (
    <li
      {...rowLink}
      className={cx("factory-board__row", rowLink.className)}
      data-selected={selected || undefined}
    >
      {/* `role="img"`, so the colour is announced rather than silently skipped: a bare span has
          no role for an `aria-label` to attach to, and it is real information here — the lane
          when the board is coloured by lane, and the ticket's own figure when it is not. */}
      <span
        className="factory-board__row-lane"
        data-look={look}
        role="img"
        aria-label={metric === null ? LANE_LABELS[lane] : spend}
      />
      {/* The link is also the row's flexible gap — it takes the width the number no longer
          claims, so a ticket with no stored title still pushes the metric and the project to the
          right edge. The title is elided by CSS rather than cut here, and whole on the tooltip. */}
      <Tooltip content={rowFacts(node, metric)} describes position="top-start">
        <Link to={`/tickets/${node.id}`} className="factory-board__row-link">
          <span className="factory-board__row-key">{issueNumber(node.key)}</span>
          <span className="factory-board__row-title">{node.title}</span>
        </Link>
      </Tooltip>
      {node.sandboxWaiting && <Lozenge>Waiting for a sandbox</Lozenge>}
      {/* The number behind the band the swatch shows. */}
      {spend && <span className="factory-board__row-metric">{spend}</span>}
      {project !== null && <span className="factory-board__row-project">{project}</span>}
    </li>
  );
}

function subtitle(focus: Focus): string {
  return focus.kind === "project" ? "Every ticket in this project" : LANE_HINTS[focus.lane];
}

/** The project a focus has narrowed to, or null for a lane focus — which spans all of them — and
 *  for no focus at all. */
function focusedProject(focus: Focus | null): number | null {
  return focus === null || focus.kind === "lane" ? null : focus.projectId;
}
