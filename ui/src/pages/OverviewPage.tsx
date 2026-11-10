/**
 * The factory board — the whole factory on one screen, at 40,000 feet.
 *
 * The columns are ticket state, not ADLC steps. Every project brings its own `process.yaml` and
 * changes it over time, so a step-keyed column cannot mean the same thing for every node on a
 * board that spans projects — see `components/factory-board/model.ts`.
 *
 * Three controls, on three axes, and none of them clears another:
 *
 *   - The METRIC buttons decide what colour a lit node is. Each replaces the lane palette with
 *     one metric's ramp — retries, dollars, runtime, tokens — and ranks the worst projects to the
 *     top, answering "where is the factory struggling, and at what". They are hover previews with
 *     a click to pin, because the question is usually a glance.
 *   - FOCUS decides which nodes are lit. Everything outside the clicked lane, project, or node
 *     group mutes, the pips bracket it, and the list beside the board holds what it selected.
 *   - The caret on a lane heading decides the ORDER of the project rows: how many tickets each
 *     project has in that one lane, most first or fewest first.
 *
 * Composed, they answer the narrower question the pair implies — what is THIS project spending —
 * and no control has to be given up to ask it. The focus even survives the re-ranking: it is
 * stored as a project id, not a row, so the pips follow the project wherever it lands.
 *
 * That composition is why the metric buttons report on the FOCUSED set rather than on the board:
 * click a project and all four say what it costs, how long it has run, how many tokens it has
 * burned and how often it has retried, before any shading is turned on.
 *
 * The header says what is applied and takes it off again, and the address holds the same three
 * things (`useBoardView`), so a view can be linked. The strip under the header is the reading
 * instruments; below them the board and the issue list sit side by side, each in its own frame.
 *
 * A phone draws the issue list alone: a board is a picture that needs the width, and a phone's
 * task here is finding a ticket and opening it (docs/design-system.md, "Phone width"). A linked
 * view still narrows the list, and the header still says so and takes it off.
 */

import { useId, useMemo, useState } from "react";
import { useCan } from "../api/organizationQueries";
import { useOverview } from "../api/queries";
import { ActiveFilter, shows, useActiveView } from "../components/configuration/ActiveFilter";
import {
  AppliedRow,
  Board,
  DEFAULT_DENSITY,
  DENSITIES,
  focusedNodes,
  HeatLegend,
  IssueList,
  LANES,
  METRIC_SPECS,
  ModeBar,
  metricOf,
  modeButtons,
  SettingsDrawer,
  SHADE_MODES,
  StatsPanel,
  samePlace,
  toBoard,
  useBoardPrefs,
  useBoardView,
} from "../components/factory-board";
import {
  Button,
  EmptyState,
  ErrorMessage,
  LinkButton,
  PageHeader,
  Panel,
  Spinner,
  StaleNote,
  staleSince,
  usePageTitle,
} from "../components/ui";
import { useIsPhone } from "../components/ui/media";

export function OverviewPage() {
  const query = useOverview();
  const { data, isPending, error } = query;
  const canCreate = useCan("projects:manage");
  const phone = useIsPhone();
  usePageTitle(["Overview"]);
  const appliedId = useId();
  const boardHeadingId = useId();
  const [admission, setAdmission] = useActiveView();
  const { nodes, projects } = useMemo(() => {
    if (!data) return { nodes: [], projects: [] };
    const shown = data.projects.filter((project) => shows(admission, project.active));
    return toBoard({ ...data, projects: shown });
  }, [data, admission]);
  // The two things Settings sets, restored from the last visit. Everything else on this page is
  // what is APPLIED right now, is cleared by Reset view, and is deliberately not remembered.
  const { prefs, setThresholds, setDensity } = useBoardPrefs();
  // What is applied — the pinned metric, the focus, the lane heading ranking the project rows —
  // lives in the address rather than here, and the applied-state row reports all three.
  const { view, focus, laneSort, setFocus, setLaneSort, reset } = useBoardView();
  // The ticket whose square was last clicked. It is not a second focus — the board is still
  // narrowed by `focus` alone — it is the reader's place in the list beside it.
  const [selected, setSelected] = useState<number | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const { thresholds, density } = prefs;
  const size = DENSITIES[density] ?? DENSITIES[DEFAULT_DENSITY];
  const metric = metricOf(view.effective);

  /**
   * The selection. With no focus this is the whole board rather than nothing: the list is the
   * readable form of what the board draws, so it has something to say before any selection is
   * made, and focusing only ever narrows it. It is also what the metric buttons report on, which
   * is why there is one array and not two — the figures on the rail and the rows in the list can
   * never disagree about what is selected.
   */
  const listed = useMemo(() => focusedNodes(focus, nodes), [nodes, focus]);

  const buttons = useMemo(() => modeButtons(SHADE_MODES, listed), [listed]);

  const header = (
    <PageHeader
      title="Overview"
      actions={
        !phone && (
          <Button iconBefore="settings" onClick={() => setSettingsOpen(true)}>
            Board settings
          </Button>
        )
      }
      bottomBar={
        <>
          <ActiveFilter value={admission} onChange={setAdmission} projects={data?.projects ?? []} />
          <AppliedRow
            id={appliedId}
            metric={metricOf(view.locked)}
            focus={focus}
            laneSort={laneSort}
            projects={projects}
            onClearMetric={() => view.controller.clear()}
            onClearFocus={() => {
              setFocus(null);
              setSelected(null);
            }}
            onClearSort={() => setLaneSort(null)}
            onReset={() => {
              reset();
              setSelected(null);
            }}
          />
          {/* In the row, not under it: a failed refetch keeps the board, whose contract is that
              nothing moves, so the line that says the data is stale must not push it down. */}
          <StaleNote updatedAt={staleSince(query)} />
        </>
      }
    />
  );

  const settings = settingsOpen && (
    <SettingsDrawer
      thresholds={thresholds}
      onThresholds={setThresholds}
      density={density}
      onDensity={setDensity}
      onClose={() => setSettingsOpen(false)}
    />
  );

  // Three states the prototype could never be in, because its data was generated in the browser.
  // An empty factory gets a sentence rather than a board of vacant lattice, which reads as a bug.
  // A failed refetch is not one of them: the board keeps its last answer.
  if (isPending || (error && !data) || projects.length === 0) {
    return (
      <div className="space-y-6">
        {header}
        <Panel>
          {isPending ? (
            <div className="p-6">
              <Spinner label="Loading the factory" />
            </div>
          ) : error && !data ? (
            <ErrorMessage
              error={error}
              title="Couldn't load the factory"
              onRetry={() => query.refetch()}
              className="m-4"
            />
          ) : data && data.projects.length > 0 ? (
            <EmptyState
              header="No projects match this filter"
              primaryAction={<Button onClick={() => setAdmission("all")}>Show all projects</Button>}
            />
          ) : (
            <EmptyState
              header="No projects yet"
              description={
                canCreate
                  ? "Create a project and its tickets appear here."
                  : "When an admin creates a project, its tickets appear here."
              }
              primaryAction={
                canCreate && (
                  <LinkButton to="/projects/new" appearance="primary">
                    Create project
                  </LinkButton>
                )
              }
            />
          )}
        </Panel>
        {settings}
      </div>
    );
  }

  const list = (
    <IssueList
      focus={focus}
      nodes={listed}
      projects={projects}
      // So a row's swatch and its square on the board are painted by the same decision.
      mode={view.effective}
      thresholds={thresholds}
      selected={selected}
    />
  );

  if (phone) {
    return (
      <div className="flex h-full flex-col gap-3">
        {header}
        <div className="flex min-h-80 flex-1 flex-col">{list}</div>
      </div>
    );
  }

  // A fill page (`routes.tsx`): the two frames share whatever height the header and the
  // instruments leave, so they end level at the foot of the window with nothing measured.
  return (
    <div className="flex h-full flex-col gap-4">
      {header}

      <div className="factory-board__instruments">
        <StatsPanel issues={nodes.length} projects={projects.length} />
        <ModeBar buttons={buttons} mode={view} />
        <HeatLegend metric={metric} thresholds={thresholds} />
      </div>

      <div className="factory-board__stage">
        <section
          aria-labelledby={boardHeadingId}
          data-assist-anchor="overview.board"
          className="factory-board__panel factory-board__frame rounded-lg border border-default"
        >
          <header className="border-b border-default bg-surface px-4 py-3">
            <h2 id={boardHeadingId} className="text-heading-xs text-default">
              Board
            </h2>
            {/* What the colours mean right now, hover preview included — nothing else on the page
                says so, and the shading changes on a hover. One line, always, so the board under
                it cannot move. */}
            <p className="factory-board__note text-body-sm" data-on={metric !== null || undefined}>
              {metric === null ? "Colored by lane" : `Shaded by ${METRIC_SPECS[metric].noun}`}
            </p>
          </header>
          <div className="factory-board__frame-body">
            <Board
              nodes={nodes}
              projects={projects}
              lanes={LANES}
              // The preview if the cursor is resting on a button, the pinned mode otherwise.
              mode={view.effective}
              focus={focus}
              // A second click on the same target clears the focus, so every control is its own
              // way back out — EXCEPT when a ticket's own square was what was clicked. There the
              // click means "this one", and answering it by clearing the focus would take away
              // the selection the reader was pointing at. Clicking the same cell twice still
              // clears; clicking two tickets in it in turn moves between them.
              onFocus={(next, ticket) => {
                setFocus(ticket == null && samePlace(focus, next) ? null : next);
                // Anything that is not a ticket ends the selection, because the row it marked is
                // about to stop being in the list.
                setSelected(ticket ?? null);
              }}
              laneSort={laneSort}
              onLaneSort={setLaneSort}
              thresholds={thresholds}
              nodeSize={size.node}
              gap={size.gap}
            />
          </div>
        </section>

        {list}
      </div>

      {settings}
    </div>
  );
}
