/**
 * The board's structural invariants — the ones about markup rather than about ordering, so
 * `layout.test.ts` cannot reach them.
 *
 * Rendered with `renderToString`: no DOM, no new dependency, and everything asserted here is in
 * the first paint. What it is really guarding is the muting contract, because that is the one
 * rule split across two files — `slotsOf` decides what mutes, and the stylesheet reads
 * `data-look="muted"`.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { keys } from "../../api/queries";
import { seedFailure } from "../../api/queryTesting";
import type { OverviewResponse } from "../../api/types";
import { OverviewPage } from "../../pages/OverviewPage";
import { describe as describeSlot, VacantBand } from "./Band";
import { Board, bandCapacity } from "./Board";
import { describeFocus, type Focus } from "./focus";
import { type LaneSort, layout } from "./layout";
import { type IssueNode, LANES, type ProjectBand } from "./model";

const PROJECTS: ProjectBand[] = [
  { id: 1, name: "alpha" },
  { id: 2, name: "beta" },
];

const NODES: IssueNode[] = [
  {
    id: 1,
    key: "a#1",
    title: null,
    projectId: 1,
    step: "build",
    latestRunStatus: null,
    status: "running",
    runs: 1,
    attempt: 1,
    costUsd: 0,
    minutes: 0,
    tokens: 0,
  },
  {
    id: 2,
    key: "a#2",
    title: null,
    projectId: 1,
    step: "build",
    latestRunStatus: null,
    status: "blocked",
    runs: 3,
    attempt: 3,
    costUsd: 0,
    minutes: 0,
    tokens: 0,
  },
  {
    id: 3,
    key: "a#3",
    title: null,
    projectId: 2,
    step: "spec_gate",
    latestRunStatus: null,
    status: "waiting_gate",
    runs: 1,
    attempt: 1,
    costUsd: 0,
    minutes: 0,
    tokens: 0,
  },
];

/** Every retry sits in the second-declared project, so a shade mode has a reason to re-rank. */
const RETRIED: IssueNode[] = [
  {
    id: 1,
    key: "a#1",
    title: null,
    projectId: 1,
    step: "build",
    latestRunStatus: null,
    status: "running",
    runs: 1,
    attempt: 1,
    costUsd: 0,
    minutes: 0,
    tokens: 0,
  },
  {
    id: 4,
    key: "b#4",
    title: null,
    projectId: 2,
    step: "build",
    latestRunStatus: null,
    status: "running",
    runs: 5,
    attempt: 4,
    costUsd: 0,
    minutes: 0,
    tokens: 0,
  },
];

/** The project labels down the page, in the order the board draws them. */
function bandOrder(html: string): string[] {
  return [...html.matchAll(/factory-board__gutter-name">([^<]*)/g)].map((match) => match[1] ?? "");
}

/** The label of the band whose gutter is marked focused, if any. */
function focusedBand(html: string): string | undefined {
  for (const chunk of html.split('class="factory-board__gutter"').slice(1)) {
    const attributes = chunk.slice(0, chunk.indexOf(">"));
    if (attributes.includes('data-focused="true"')) {
      return chunk.match(/factory-board__gutter-name">([^<]*)/)?.[1];
    }
  }
  return undefined;
}

function render(
  mode: Parameters<typeof Board>[0]["mode"],
  focus: Focus | null = null,
  laneSort: LaneSort | null = null,
) {
  return renderToString(
    <Board
      nodes={NODES}
      projects={PROJECTS}
      lanes={LANES}
      mode={mode}
      focus={focus}
      onFocus={() => {}}
      laneSort={laneSort}
      onLaneSort={() => {}}
      nodeSize={4}
      gap={2}
    />,
  );
}

const count = (html: string, needle: string) => html.split(needle).length - 1;

/** Every metric on the page, as the two halves of its box in the order they are written. */
function metrics(html: string): string[][] {
  return html
    .split("<span data-metric-box")
    .slice(1)
    .map((chunk) => {
      const unit = chunk.match(/data-metric-unit[^>]*>([^<]*)</)?.[1] ?? "";
      const value = chunk.match(/data-metric-value[^>]*>([^<]*)</)?.[1] ?? "";
      return chunk.indexOf("data-metric-unit") < chunk.indexOf("data-metric-value")
        ? [unit, value]
        : [value, unit];
    });
}

/** Each lane cell's whole opening tag, in document order. */
function laneCells(html: string): { tag: string; label: string; inert: boolean }[] {
  return [...html.matchAll(/<button[^>]*class="factory-board__lane"[^>]*>/g)].map((match) => ({
    tag: match[0],
    label: match[0].match(/aria-label="([^"]*)"/)?.[1] ?? "",
    inert: match[0].includes('aria-disabled="true"'),
  }));
}

describe("Board", () => {
  it("draws every position on the board, empty ones included", () => {
    // Occupancy has to be an appearance rather than the presence of an element: re-ranking the
    // bands can empty a position, and a disappearing element would pop instead of fading.
    const html = render("idle");
    const board = layout(NODES, PROJECTS, LANES, "idle");
    expect(count(html, 'class="factory-board__slot"')).toBe(
      PROJECTS.length * LANES.length * board.rows * 10,
    );
    expect(count(html, 'data-look="empty"')).toBe(
      PROJECTS.length * LANES.length * board.rows * 10 - NODES.length,
    );
  });

  it("mutes nothing while idle", () => {
    expect(render("idle")).not.toContain('data-look="muted"');
  });

  it("paints each node with its lane, so colour and column cannot disagree", () => {
    const html = render("idle");
    expect(count(html, 'data-look="running"')).toBe(1);
    expect(count(html, 'data-look="error"')).toBe(1);
    expect(count(html, 'data-look="gate"')).toBe(1);
  });

  it("gives every lane a heading that can be clicked into focus", () => {
    const html = render("idle");
    expect(count(html, 'class="factory-board__lane-head"')).toBe(LANES.length);
    expect(html).toContain("Retrying");
    expect(html).toContain("Admitted");
  });

  it("gives every heading a sort of its own, off until it is pressed", () => {
    // The heading is a pair of controls, not one: the name focuses the column and the caret ranks
    // the project rows by it, and a reader has to be able to ask either without asking the other.
    const html = render("idle");
    expect(count(html, 'class="factory-board__lane-sort"')).toBe(LANES.length);
    expect(html).toContain('aria-label="Sort projects by Gate tickets"');
    expect(html).not.toContain("data-on");
  });

  it("ranks the project rows by how many tickets they have in the sorted lane", () => {
    // alpha holds the two running/blocked tickets and beta holds the only one at a gate, so
    // sorting on Gate has to lift beta over the declared order, and reversing it puts it back.
    expect(bandOrder(render("idle", null, { lane: "gate", dir: "desc" }))).toEqual([
      "beta",
      "alpha",
    ]);
    expect(bandOrder(render("idle", null, { lane: "gate", dir: "asc" }))).toEqual([
      "alpha",
      "beta",
    ]);
    // And with nothing sorted it is the order the projects were declared in.
    expect(bandOrder(render("idle"))).toEqual(["alpha", "beta"]);
  });

  it("lets a lane sort outrank a shade mode's own ordering", () => {
    // beta carries every retry — its one ticket is on attempt 4, which puts it in Retrying — so
    // `shade_retry` alone lifts beta over alpha. alpha holds the only ticket in Running, so a sort
    // on that heading wants the opposite order. A press on a heading is a deliberate act and a
    // metric preview arrives on a hover, so the press has to win.
    const draw = (laneSort: LaneSort | null) =>
      renderToString(
        <Board
          nodes={RETRIED}
          projects={PROJECTS}
          lanes={LANES}
          mode="shade_retry"
          focus={null}
          onFocus={() => {}}
          laneSort={laneSort}
          onLaneSort={() => {}}
          nodeSize={4}
          gap={2}
        />,
      );
    expect(bandOrder(draw(null))).toEqual(["beta", "alpha"]);
    expect(bandOrder(draw({ lane: "running", dir: "desc" }))).toEqual(["alpha", "beta"]);
  });

  it("mutes every lane but the focused one", () => {
    const html = render("idle", { kind: "lane", lane: "gate" });
    expect(count(html, 'data-look="muted"')).toBe(2);
    expect(count(html, 'data-look="gate"')).toBe(1);
    expect(html).toContain('aria-pressed="true"');
  });

  it("pips the two ends of a focused column and nothing between", () => {
    const html = render("idle", { kind: "lane", lane: "gate" });
    // Two projects, so both bands are an end. The pips bracket the column; the heading names it.
    expect(count(html, 'class="factory-board__reticle"')).toBe(2);
  });

  it("pips the two ends of a focused row", () => {
    const html = render("idle", { kind: "project", projectId: 1 });
    expect(count(html, 'class="factory-board__reticle"')).toBe(2);
    expect(count(html, 'data-look="muted"')).toBe(1);
  });

  it("pips one node group for a cell focus, and draws nothing spanning", () => {
    const html = render("idle", { kind: "cell", lane: "error", projectId: 1 });
    expect(count(html, 'class="factory-board__reticle"')).toBe(1);
  });

  it("draws four corner triangles for a cell focus", () => {
    const html = render("idle", { kind: "cell", lane: "error", projectId: 1 });
    expect(count(html, 'class="factory-board__reticle-corner"')).toBe(4);
  });

  it("splits one rectangle's four corners across a column's two ends", () => {
    // Two reticles, two pips each: the rectangle spans the column rather than boxing each end.
    const html = render("idle", { kind: "lane", lane: "gate" });
    expect(count(html, 'class="factory-board__reticle"')).toBe(2);
    expect(count(html, 'class="factory-board__reticle-corner"')).toBe(4);
  });

  it("splits one rectangle's four corners across a row's two ends", () => {
    const html = render("idle", { kind: "project", projectId: 1 });
    expect(count(html, 'class="factory-board__reticle"')).toBe(2);
    expect(count(html, 'class="factory-board__reticle-corner"')).toBe(4);
  });

  it("gives every CORNER its own phase delay, not one per reticle", () => {
    // Corners outlive reticles: narrowing a column focus to one of its groups keeps that
    // group's pips and adds the two the other end held. A delay carried by the parent would
    // put those arrivals out of step, which is the drift this guards.
    const html = render("idle", { kind: "lane", lane: "gate" });
    expect(count(html, "--fb-reticle-delay")).toBe(4);
    expect(count(html, 'class="factory-board__reticle"')).toBe(2);
  });

  it("boxes the node group rather than the wider lane cell", () => {
    const html = render("idle");
    expect(count(html, 'class="factory-board__group"')).toBe(PROJECTS.length * LANES.length);
  });

  it("names the retrying lane Retrying", () => {
    expect(render("idle")).toContain("Retrying");
  });

  it("paces a focus change faster than a mode change", () => {
    expect(render("idle")).toContain('data-pace="click"');
  });

  it("puts each lane's count under its label", () => {
    const html = render("idle");
    expect(html).toContain('class="factory-board__lane-name"');
    expect(html).toContain('class="factory-board__lane-count"');
  });

  it("does not fade a project gutter that is not being swapped out", () => {
    // The gutters blinked on every click when this was keyed off "a crossfade is running"
    // rather than off this band's project actually changing.
    expect(render("idle", { kind: "lane", lane: "gate" })).not.toContain(
      'class="factory-board__gutter" data-fading',
    );
  });

  it("names the focused project and lane by marking their headings", () => {
    const html = render("idle", { kind: "cell", lane: "error", projectId: 1 });
    // Both headings, and only one of each: the pips say where, the headings say what.
    expect(count(html, 'data-focused="true"')).toBeGreaterThanOrEqual(2);
  });

  it("gives every node group its own click target, labelled for a screen reader", () => {
    const html = render("idle");
    expect(count(html, 'class="factory-board__lane"')).toBe(PROJECTS.length * LANES.length);
    expect(html).toContain("alpha, Error, 1 ticket");
  });

  it("boxes the unit of a gutter's metric, as a ticket page boxes its own", () => {
    // The band load is the one figure the board itself prints; the units are set apart there for
    // the same reason they are on a ticket — the reader is comparing the numbers, not the marks.
    expect(metrics(render("shade_cost"))).toContainEqual(["$", "0.00"]);
    // A count has no unit, and its noun is not one: the idle gutter stays as it reads.
    expect(metrics(render("idle"))).toEqual([]);
  });

  it("makes the project gutter a control of its own", () => {
    expect(count(render("idle"), 'class="factory-board__gutter"')).toBe(PROJECTS.length);
  });

  it("mutes nothing in a shade mode, so the ramp is visible across every node", () => {
    const html = render("shade_retry");
    expect(html).toContain('data-mode="shade_retry"');
    expect(html).not.toContain('data-look="muted"');
    expect(count(html, 'data-look="heat-2"')).toBe(1);
  });

  it("shades the focused nodes and mutes the rest, so shading and focus compose", () => {
    // The two controls are on different axes and neither cancels the other: shading decides what
    // colour a lit node is, the focus decides which nodes are lit.
    const html = render("shade_retry", { kind: "project", projectId: 1 });
    expect(html).toContain('data-mode="shade_retry"');
    expect(count(html, 'data-look="heat-2"')).toBe(1);
    expect(count(html, 'data-look="heat-0"')).toBe(1);
    expect(count(html, 'data-look="muted"')).toBe(1);
  });

  it("keeps a project focus on its project when a shade mode re-ranks the bands", () => {
    // beta carries every retry here, so the retry mode lifts it above alpha. The focus is a project id
    // rather than a row, so it has to follow alpha down instead of staying on the top band.
    const html = renderToString(
      <Board
        nodes={RETRIED}
        projects={PROJECTS}
        lanes={LANES}
        mode="shade_retry"
        focus={{ kind: "project", projectId: 1 }}
        onFocus={() => {}}
        laneSort={null}
        onLaneSort={() => {}}
        nodeSize={4}
        gap={2}
      />,
    );
    expect(bandOrder(html)).toEqual(["beta", "alpha"]);
    expect(focusedBand(html)).toBe("alpha");
  });

  it("carries the lane index on each lane, which is what staggers the sweep", () => {
    expect(render("idle")).toContain("--fb-lane:");
  });

  it("never positions anything, so no square can be moved by a re-rank", () => {
    const html = render("idle");
    expect(html).not.toContain("translate");
    expect(html).not.toContain("position:absolute");
  });

  it("starts settled, with nothing mid-fade on first paint", () => {
    expect(render("idle")).not.toContain("data-fading");
  });
});

const WIRE: OverviewResponse["tickets"][number] = {
  ticket_id: 1,
  project_id: 1,
  external_key: "acme/repo#12",
  title: null,
  current_step: "build",
  process_key: "standard",
  latest_run_status: null,
  park_reason: null,
  status: "running",
  runs: 3,
  attempt: 1,
  cost_usd: "4.25",
  cost_estimated: false,
  tokens: 120_000,
  minutes: 45.5,
  quarantined_at: null,
};

/**
 * The page against a pre-seeded query cache, the way `TicketPage.test.tsx` does it: no fetch, and
 * the answer is there synchronously so the page renders its real content instead of a spinner.
 *
 * The router is not optional — the issue list shows the whole board by default, so every row
 * carries a real `<Link>` and `useRowLink`.
 */
const page = (data: OverviewResponse, refetchFailed = false, url = "/overview") => {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, retryOnMount: false } },
  });
  qc.setQueryData(keys.overview, data);
  if (refetchFailed) seedFailure(qc, keys.overview, "cannot reach the API", data);
  return renderToString(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}>
        <OverviewPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
};

const BOARD: OverviewResponse = {
  projects: [{ id: 1, name: "widgets", active: true }],
  tickets: [WIRE],
  done_days: 7,
};

describe("OverviewPage", () => {
  it("says the factory is empty rather than drawing an empty board", () => {
    // A board of nothing but vacant lattice looks like a bug. With no projects there is nothing
    // to draw bands for, and the answer is a sentence.
    const html = page({ projects: [], tickets: [], done_days: 7 });
    expect(html).toContain("No projects yet");
    expect(html).not.toContain('class="factory-board__band"');
  });

  it("draws a band per project once there is something to draw", () => {
    const html = page(BOARD);
    expect(html).toContain("Overview");
    expect(html).toContain("widgets");
    expect(html).toContain('class="factory-board__slot"');
  });

  it("offers the four shade modes, and only those, as views", () => {
    // The filter modes are the lanes wearing a button; what each shade mode asks is the one
    // question no arrangement of the columns answers.
    const html = page(BOARD);
    expect(count(html, "data-tone=")).toBe(4);
    for (const tone of ["shade_retry", "shade_cost", "shade_time", "shade_tokens"]) {
      expect(html).toContain(`data-tone="${tone}"`);
    }
    expect(html).not.toContain('data-tone="blocked"');
  });

  it("keeps the board when a refetch fails, and says the figures are stale", () => {
    // The board's contract is that nothing moves; a network blip used to swap it for a sentence.
    const html = page(BOARD, true);
    expect(html).toContain('class="factory-board__bar"');
    expect(html).not.toContain("could not be read");
    expect(html).toContain("Couldn&#x27;t refresh this page. Showing data from");
  });

  it("opens with nothing applied, and a Reset that says why it does nothing", () => {
    const html = page(BOARD);
    // Mounted and focusable, but inert, and pointing at the words that say nothing is applied.
    const reset = html.match(/<button[^>]*aria-describedby="([^"]+)"[^>]*>(?:<[^>]+>)*Reset view/);
    expect(reset?.[0]).toContain('aria-disabled="true"');
    const applied = html.match(new RegExp(`id="${reset?.[1]}"[^>]*>(.*?)</div>`));
    expect(applied?.[1]).toContain("Nothing is applied to the board.");
    // One control per thing applied, and none while nothing is.
    expect(html).not.toContain("Clear metric");
    expect(html).not.toContain("Clear focus");
  });

  it("heads the page like every other, with the settings behind one button", () => {
    const html = page(BOARD);
    expect(html).toMatch(/<h1[^>]*data-page-title[^>]*>Overview<\/h1>/);
    expect(html).toContain("Board settings");
    expect(html).toContain('data-glyph="settings"');
    // The node size is not up here: it is set once rather than glanced at, so it lives in the
    // drawer the button opens.
    expect(html).not.toContain('aria-label="Ticket size"');
    for (const shout of ["SETTINGS", "METRICS", "FOCUS", "RESET"]) {
      expect(html).not.toContain(`>${shout}<`);
    }
  });

  it("opens a linked view as it was linked: shaded, focused and ranked", () => {
    // The address holds what is applied, so a view can be sent to someone and survives a reload.
    const html = page(BOARD, false, "/overview?metric=cost&focus=lane:running&sort=error-desc");
    expect(html).toContain('data-mode="shade_cost"');
    expect(html).toContain("Metric: <!-- -->Cost");
    expect(html).toContain("Focus: <!-- -->Running");
    expect(html).toContain("Sort: <!-- -->Error");
    // Pinned from the address, so its button reads as pressed.
    expect(html).toMatch(
      /aria-pressed="true"[^>]*data-tone="shade_cost"|data-tone="shade_cost"[^>]*aria-pressed="true"/,
    );
    expect(html).not.toContain("Nothing is applied to the board.");
  });

  it("opens plain on an address it cannot read, rather than on a guess", () => {
    const html = page(BOARD, false, "/overview?metric=price&focus=lane:blocked&sort=error-up");
    expect(html).toContain('data-mode="idle"');
    expect(html).toContain("Nothing is applied to the board.");
  });

  it("puts the metric buttons on the instrument strip above the board", () => {
    const html = page(BOARD);
    expect(html).toContain('class="factory-board__instruments"');
    expect(html).toContain('class="factory-board__bar"');
  });

  it("stands the board and the issue list in separate frames, side by side", () => {
    const html = page(BOARD);
    expect(html).toContain('class="factory-board__stage"');
    expect(count(html, "factory-board__panel")).toBe(2);
    // Framed by the role border, not a palette step, and neither frame lifts off the page.
    expect(count(html, "rounded-lg border border-default")).toBe(2);
    expect(html).not.toContain("shadow-sm");
    expect(html).not.toContain("slate-");
    // The board's frame says what the colours mean, and keeps that line when nothing is shaded
    // so the board under it cannot move.
    expect(html).toContain("Colored by lane");
    // Mounted with nothing focused, and already holding the whole board — so a click narrows
    // the list rather than conjuring it, and never resizes the board beside it.
    expect(html).toContain("Every ticket");
    expect(html).toContain('class="factory-board__row cursor-pointer"');
  });

  it("counts the whole factory beside the metric buttons", () => {
    const html = page(BOARD);
    expect(html).toContain('class="factory-board__stats"');
    expect(html).toContain("projects");
  });

  it("starts on the lane colours, with the ramp key waiting rather than empty", () => {
    const html = page(BOARD);
    expect(html).toContain('data-mode="idle"');
    expect(html).toContain("Select a metric");
    // Present but hidden, so picking a metric cannot change the strip's height.
    expect(count(html, 'class="factory-board__legend-swatch"')).toBe(4);
  });
});

describe("bandCapacity", () => {
  it("counts the bands that fit, gaps included, and rounds down", () => {
    // Six 40px bands and five 12px gaps is 300px exactly. One pixel short and the sixth band
    // would be a band the reader has to scroll to see, which the panel is sized to prevent.
    expect(bandCapacity(300, 40, 12)).toBe(6);
    expect(bandCapacity(299, 40, 12)).toBe(5);
    // The last band needs no gap after it.
    expect(bandCapacity(40, 40, 12)).toBe(1);
  });

  it("asks for nothing when there is no room, or nothing to measure", () => {
    expect(bandCapacity(0, 40, 12)).toBe(0);
    expect(bandCapacity(-80, 40, 12)).toBe(0);
    // Before the first band has been measured there is no height to divide by, and a division by
    // zero here would ask for an infinity of empty bands.
    expect(bandCapacity(300, 0, 12)).toBe(0);
  });
});

describe("VacantBand", () => {
  const vacant = (rows: number) => renderToString(<VacantBand lanes={LANES} rows={rows} />);

  it("draws the same lattice as a real band, hollow", () => {
    const html = vacant(4);
    expect(count(html, "factory-board__slot--vacant")).toBe(LANES.length * 4 * 10);
    // The same three boxes a real band is built from, so the columns still line up.
    expect(count(html, 'class="factory-board__lane"')).toBe(LANES.length);
    expect(count(html, 'class="factory-board__group"')).toBe(LANES.length);
  });

  it("is not a control, and is not read out", () => {
    // There is no project here to focus, no count to report and no name to say. A band position
    // that offered a click would be offering one on nothing.
    const html = vacant(4);
    expect(html).not.toContain("<button");
    expect(html).toContain('aria-hidden="true"');
  });

  it("carries no appearance, so a mode change has nothing here to crossfade", () => {
    expect(vacant(4)).not.toContain("data-look");
  });
});

describe("the board's vacant fill", () => {
  it("draws none until it has measured, so the server render is the projects and nothing else", () => {
    // `useBandFill` needs a `ResizeObserver` and a laid-out box, and has neither here. Starting
    // at zero rather than at a guess is what keeps the first client paint identical to this.
    expect(count(page(BOARD), "factory-board__slot--vacant")).toBe(0);
  });
});

describe("an empty node group", () => {
  it("is not a target: no tab stop, no click, and nothing it could select", () => {
    // A cell with no issues in it is a picture of absence. Left clickable it offers a focus that
    // would select nothing, and on a board of six lanes per project the empty ones are most of
    // the tab stops on the page.
    const cells = laneCells(render("idle"));
    expect(cells).toHaveLength(PROJECTS.length * LANES.length);
    for (const cell of cells) {
      expect(cell.inert).toBe(cell.label.endsWith("0 tickets"));
      expect(cell.tag.includes('tabindex="-1"')).toBe(cell.inert);
    }
  });

  it("says so with aria-disabled, never the disabled attribute", () => {
    // `disabled` is the obvious way to write this and it is a trap: the theme gives a disabled
    // BUTTON an inset border so it still reads as a control, and a lane cell is exactly as wide
    // as its node group — so that border drew a box tight around the nodes of every empty cell.
    for (const cell of laneCells(render("idle"))) {
      expect(cell.tag).not.toMatch(/\sdisabled[\s=>]/);
    }
  });

  it("leaves every cell that holds something exactly as it was", () => {
    const holding = laneCells(render("idle")).filter((cell) => !cell.inert);
    expect(holding.map((cell) => cell.label)).toEqual([
      "alpha, Running, 1 ticket",
      "alpha, Error, 1 ticket",
      "beta, Gate, 1 ticket",
    ]);
  });
});

describe("clicking a square", () => {
  it("labels every filled slot with its ticket, and no empty one", () => {
    // The seam the cell's click handler reads: the slots are not buttons, so which ticket was hit
    // is a question answered from the DOM rather than from a per-square handler.
    const html = render("idle");
    expect(count(html, "data-ticket=")).toBe(NODES.length);
    for (const node of NODES) expect(html).toContain(`data-ticket="${node.id}"`);
  });
});

describe("describeFocus", () => {
  const projects = [
    { id: 1, name: "widgets" },
    { id: 2, name: "payments-api" },
  ];

  it("names a single selection by the one thing it selected", () => {
    expect(describeFocus({ kind: "lane", lane: "gate" }, projects)).toBe("Gate");
    expect(describeFocus({ kind: "project", projectId: 2 }, projects)).toBe("payments-api");
  });

  it("reads a cell as a path into the board, project first", () => {
    // Narrowing rightwards rather than two equal halves joined by a plus — and the same order the
    // ticket list's own heading uses, so the two panels describe one selection the same way.
    expect(describeFocus({ kind: "cell", lane: "error", projectId: 1 }, projects)).toBe(
      "widgets / Error",
    );
  });

  it("says something rather than nothing for a project the board is not drawing", () => {
    // A focus outlives a re-rank by holding a project id, so an id with no band is reachable.
    expect(describeFocus({ kind: "project", projectId: 99 }, projects)).toBe("Project");
  });
});

describe("a square's hover text", () => {
  it("is one delegated bubble, never a native title on each of the couple of thousand squares", () => {
    // A `title` shows to a mouse alone and says nothing to anyone else; the cell reads its own
    // hover and the board draws one bubble, and every fact in it is in the issue list too.
    expect(render("idle")).not.toContain("title=");
  });

  it("names the ticket, its step and its lane, and the figure once a metric shades it", () => {
    const node = { ...(NODES[0] as IssueNode), title: "Fix the thing", costUsd: 4 };
    const slot = { node, appearance: "running" } as Parameters<typeof describeSlot>[0];
    const idle = describeSlot(slot, "running", "idle");
    // Named the way people name an issue, not by the key as stored.
    expect(idle?.startsWith("#1\n")).toBe(true);
    expect(idle).toContain("Fix the thing");
    expect(idle).toContain(`${node.step} · Running`);
    expect(describeSlot(slot, "running", "shade_cost")).toContain("$4.00");
  });
});
