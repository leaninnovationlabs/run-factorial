/**
 * The issue list's structural invariants, rendered with `renderToString` inside a `MemoryRouter`
 * — `useRowLink` needs a router, and there is no DOM in this test environment.
 *
 * What it mostly guards is the compaction: the list gave up three columns to fit beside the
 * board, and two of them had to survive in another form rather than simply go.
 */

import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import type { Focus } from "./focus";
import { IssueList } from "./IssueList";
import { issueNumber, rowFacts } from "./list";
import type { IssueNode, Mode, ProjectBand } from "./model";

const PROJECTS: ProjectBand[] = [
  { id: 1, name: "widgets" },
  { id: 2, name: "payments-api" },
];

const NODES: IssueNode[] = [
  {
    id: 41,
    key: "acme/widgets#118",
    title: "Retry the webhook receiver",
    projectId: 1,
    step: "spec_gate",
    latestRunStatus: null,
    status: "waiting_gate",
    runs: 2,
    attempt: 1,
    costUsd: 0,
    minutes: 0,
    tokens: 0,
  },
  {
    id: 42,
    key: "leaninnovationlabs/payments-api#204",
    title: "Refund rounding is a cent out",
    projectId: 2,
    step: "code_gate",
    latestRunStatus: null,
    status: "running",
    runs: 3,
    attempt: 2,
    costUsd: 0,
    minutes: 0,
    tokens: 0,
  },
];

/** Enough tickets to fill three pages at the twelve-row fallback — the paging tests' fixture. */
const MANY: IssueNode[] = Array.from({ length: 30 }, (_, index) => ({
  ...(NODES[0] as IssueNode),
  id: 100 + index,
  key: `acme/widgets#${100 + index}`,
}));

const count = (html: string, needle: string) => html.split(needle).length - 1;

/** `renderToString` separates adjacent text nodes with `<!-- -->`, which lands in the middle
 *  of any sentence assembled from expressions — "25–30 of 30" among them. */
const plain = (html: string) => html.replaceAll("<!-- -->", "");

/** The opening tag of the element carrying `needle`. */
const tagWith = (html: string, needle: string) => {
  const at = html.indexOf(needle);
  return at === -1 ? "" : html.slice(html.lastIndexOf("<", at), html.indexOf(">", at) + 1);
};

const render = (
  focus: Focus | null,
  nodes: IssueNode[] = NODES,
  mode: Mode = "idle",
  selected: number | null = null,
) =>
  renderToString(
    <MemoryRouter>
      <IssueList focus={focus} nodes={nodes} projects={PROJECTS} mode={mode} selected={selected} />
    </MemoryRouter>,
  );

describe("IssueList", () => {
  it("names a lane focus by its lane", () => {
    const html = render({ kind: "lane", lane: "gate" });
    expect(html).toContain("Gate");
    expect(html).toContain("Waiting for a person&#x27;s decision");
  });

  it("names a project focus by its project", () => {
    const html = render({ kind: "project", projectId: 2 });
    expect(html).toContain("payments-api");
    expect(html).toContain("Every ticket in this project");
  });

  it("names a cell focus by both", () => {
    expect(render({ kind: "cell", lane: "error", projectId: 1 })).toContain("widgets / Error");
  });

  it("links each row to the ticket page", () => {
    const html = render({ kind: "lane", lane: "gate" });
    expect(html).toContain('href="/tickets/41"');
    expect(html).toContain('href="/tickets/42"');
  });

  it("carries the issue number and the project on one line", () => {
    const html = render({ kind: "lane", lane: "gate" });
    expect(html).toContain("#118");
    expect(html).toContain("widgets");
  });

  it("keeps the lane as a swatch painted from the board's own token", () => {
    // The column became a colour rather than going: this is what lets the eye match a row to the
    // column it just came from without reading a word.
    const html = render({ kind: "project", projectId: 1 });
    expect(html).toContain('data-look="gate"');
    // And it is still named, for anyone who cannot use the colour.
    expect(html).toContain('aria-label="Gate"');
  });

  it("follows the board onto the ramp when a metric is shading it", () => {
    // A swatch still insisting about a lane while the board beside it is coloured by cost would
    // be the one element on screen disagreeing with every square it sits next to.
    const spent = [{ ...(NODES[0] as IssueNode), costUsd: 40 }];
    const html = render({ kind: "lane", lane: "gate" }, spent, "shade_cost");
    expect(html).toContain('data-look="heat-3"');
    expect(html).not.toContain('data-look="gate"');
    // Named by its own figure now, the lane no longer being what the colour says.
    expect(html).toContain('aria-label="$40.00"');
  });

  it("wears a tilde on a cost that includes a list-price estimate", () => {
    const spent = [{ ...(NODES[0] as IssueNode), costUsd: 40, costEstimated: true }];
    const html = render({ kind: "lane", lane: "gate" }, spent, "shade_cost");
    expect(html).toContain('class="factory-board__row-metric">~$40.00');
    expect(html).toContain('aria-label="~$40.00"');
    // Only cost is ever estimated; a shaded time keeps its plain figure.
    expect(render({ kind: "lane", lane: "gate" }, spent, "shade_time")).not.toContain("~");
  });

  it("puts the ticket's own figure on the row while a metric is shading the board", () => {
    // The swatch says which of four bands the ticket landed in; the figure is the number behind
    // the band, which is what a reader comparing two rows needs — and it was only on the tooltip.
    const spent = [{ ...(NODES[0] as IssueNode), costUsd: 40 }];
    const html = render({ kind: "lane", lane: "gate" }, spent, "shade_cost");
    expect(html).toContain('class="factory-board__row-metric">$40.00');
    // It comes and goes with the shading, so an unshaded list keeps the width for the rest.
    expect(render({ kind: "lane", lane: "gate" }, spent)).not.toContain(
      "factory-board__row-metric",
    );
  });

  it("names the sort option after the metric that is shading, not 'Severity'", () => {
    // The option is only offered while a metric is on, and by then the reader has picked one by
    // name on the rail beside it. "Severity" made them work out which of the four they would get.
    expect(render(null, NODES, "shade_cost")).toContain(">Cost</option>");
    expect(render(null, NODES, "shade_time")).toContain(">Duration</option>");
    expect(render(null, NODES, "shade_cost")).not.toContain("Severity");
    expect(render(null)).not.toContain('value="metric"');
  });

  it("drops the project column when the focus already fixes it", () => {
    // Every row would carry the same word, which is a column of noise taking width the rest of
    // the row needs. A lane focus spans projects, so there it stays.
    expect(render({ kind: "lane", lane: "gate" })).toContain("factory-board__row-project");
    expect(render({ kind: "project", projectId: 1 })).not.toContain("factory-board__row-project");
    expect(render({ kind: "cell", lane: "gate", projectId: 1 })).not.toContain(
      "factory-board__row-project",
    );
  });

  it("keeps the step on the link's tooltip, the one field the narrow row could not hold", () => {
    // On a `Tooltip` on the row's link, so a keyboard reaches it; never a native `title`, which
    // shows to a mouse alone.
    const [first] = NODES as [IssueNode];
    expect(rowFacts(first, null)).toBe("Retry the webhook receiver\nspec_gate · Gate");
    expect(rowFacts({ ...first, costUsd: 40 }, "cost")).toContain("spec_gate · Gate · $40.00");
    expect(rowFacts({ ...first, title: null }, null)).toBe("spec_gate · Gate");
    expect(render({ kind: "lane", lane: "gate" })).not.toContain("title=");
  });

  it("searches with a labelled field and sorts with a labelled select", () => {
    const html = render(null);
    expect(html).toContain('aria-label="Search tickets"');
    expect(html).toContain('placeholder="Number or title"');
    expect(html).toContain('aria-label="Sort tickets by"');
    // The direction toggle names what pressing it does.
    expect(html).toMatch(/aria-label="Sort (?:ascending|descending)"/);
  });

  it("says so when a focus holds nothing, and says something else when a search does", () => {
    // Two different facts. "Nothing here" is about the board; "nothing matches" is about the
    // query, and a reader who has typed needs to know which of the two they are looking at.
    const empty = plain(render({ kind: "cell", lane: "done", projectId: 1 }, []));
    expect(empty).toContain("No tickets here");
    // No count and no pager under nothing.
    expect(empty).not.toContain(" of 0");
    expect(empty).not.toContain("Ticket pages");
  });

  it("lists the whole board when nothing is focused, rather than an invitation to click", () => {
    // The list is the readable form of what the board draws, so it has something to say before
    // any selection is made — a reader who wants a name, a number or a link is here, not
    // squinting at a 7px square.
    const html = render(null);
    expect(html).toContain("#118");
    expect(html).toContain("#204");
    expect(html).toContain("Every ticket");
    // No focus, so no project to offer a page for.
    expect(html).not.toContain("View project");
  });

  it("offers the focused project's own page, once the focus names one", () => {
    // The list says WHICH tickets; the project page is where its configuration, its ledger and
    // its full ticket table live, so the focus is worth carrying out of the board.
    for (const focus of [
      { kind: "project", projectId: 7 } as const,
      { kind: "cell", lane: "done", projectId: 7 } as const,
    ]) {
      expect(render(focus)).toContain('href="/projects/7"');
    }
  });

  it("offers no project page for a lane, which spans every project", () => {
    // A column focus has no single project behind it, and a link that picked one would be
    // answering a question the reader did not ask.
    const html = render({ kind: "lane", lane: "done" });
    expect(html).not.toContain("View project");
    expect(html).not.toContain('href="/projects/');
  });

  it("turns its pages with one pager, the same one every table uses", () => {
    // Thirty issues at twelve rows a page is three pages. One device turns them — no drag
    // handle, no meter, no second set of arrows.
    const html = render(null, MANY);
    expect(html).toContain('aria-label="Ticket pages"');
    expect(tagWith(html, 'aria-label="Page 1"')).toContain('aria-current="page"');
    expect(html).toContain('aria-label="Page 3"');
    expect(html).not.toContain('role="slider"');
    expect(html).not.toContain('role="progressbar"');
    expect(plain(html)).toContain("1 to 12 of 30");
  });

  it("offers no pager for a list that fits on one page", () => {
    // There is nowhere to turn TO, and a control offering to turn pages says the list is longer
    // than it is. The count stays.
    const html = plain(render(null));
    expect(html).not.toContain("Ticket pages");
    expect(html).toContain("1 to 2 of 2");
  });

  it("is the Tickets panel until a focus gives it a narrower name", () => {
    expect(render(null)).toContain("Tickets");
    expect(render({ kind: "project", projectId: 2 })).toContain("payments-api");
  });

  it("turns its subtitle cyan while either control is narrowing it", () => {
    // The same line, in the same state, as the one over the board: the two panels are one view,
    // and what narrowed this list is what shaded that one. At rest it is ordinary muted text.
    expect(render(null)).not.toContain("data-on");
    expect(render({ kind: "lane", lane: "gate" })).toContain('data-on="true"');
    // A metric with no focus lights it too — the list is re-ranked by severity either way.
    expect(render(null, NODES, "shade_cost")).toContain('data-on="true"');
  });

  it("marks the ticket whose square was clicked, and only that one", () => {
    const html = render(null, NODES, "idle", 42);
    expect(count(html, "data-selected")).toBe(1);
    // On the row that was asked for: the marked row is the one whose link points at it.
    expect(html).toMatch(/data-selected="true"[^>]*>.*?\/tickets\/42/s);
  });

  it("turns to the page the selected ticket is on, rather than marking a row nobody can see", () => {
    // Thirty issues at twelve rows a page, and the one that was clicked is on the third. A mark
    // on a page the reader is not looking at is not a highlight, it is a no-op.
    const many = Array.from({ length: 30 }, (_, index) => ({
      ...(NODES[0] as IssueNode),
      id: 500 + index,
      key: `acme/widgets#${500 + index}`,
    }));
    const html = render(null, many, "idle", 500);
    // Sorted by issue number descending, so #500 is the last of thirty: page three.
    expect(plain(html)).toContain("25 to 30 of 30");
    expect(tagWith(html, 'aria-label="Page 3"')).toContain('aria-current="page"');
    expect(count(html, "data-selected")).toBe(1);
  });

  it("marks nothing when nothing was clicked", () => {
    expect(render(null)).not.toContain("data-selected");
  });

  it("counts what it is showing, focused or not", () => {
    // A badge beside the heading, the way every other panel in the app spells one — and not
    // capped at 99, since a factory holds more than that and this is the count of it.
    const badge = /<h2[^>]*>.*?data-appearance="neutral"[^>]*>(\d+)</s;
    expect(render(null).match(badge)?.[1]).toBe("2");
    expect(render({ kind: "project", projectId: 2 }).match(badge)?.[1]).toBe("2");
    const many = Array.from({ length: 150 }, (_, index) => ({
      ...(NODES[0] as IssueNode),
      id: 1000 + index,
    }));
    expect(render(null, many).match(badge)?.[1]).toBe("150");
  });
});

describe("the row's title", () => {
  it("shows the issue title beside the number", () => {
    const html = plain(render(null));
    expect(html).toContain("Retry the webhook receiver");
    expect(html).toContain("Refund rounding is a cent out");
  });

  it("shows the number alone for a ticket with no stored title", () => {
    // Admitted before the observer stored titles, or already done when it shipped. A blank cell
    // would read as a bug; the number is what the row identified a ticket by before this existed.
    const untitled = [{ ...(NODES[0] as IssueNode), title: null }];
    const html = plain(render(null, untitled));
    expect(html).toContain("#118");
    expect(html).not.toContain("Retry the webhook receiver");
  });
});

describe("issueNumber", () => {
  it("drops the repo, which is already the project suffix", () => {
    expect(issueNumber("acme/widgets#118")).toBe("#118");
  });

  it("writes the API's bare issue number the way people say it", () => {
    // `ticket.external_key` is `str(issue.number)`, so this is the key production sends.
    expect(issueNumber("118")).toBe("#118");
  });

  it("leaves a key with no number alone rather than mangling it", () => {
    expect(issueNumber("some-odd-key")).toBe("some-odd-key");
  });
});
