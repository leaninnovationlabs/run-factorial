/**
 * The ticket page's structural invariants — the ones that are about markup rather than about a
 * computation, so `activity.test.ts` cannot reach them.
 *
 * Rendered with `renderToString` against a pre-seeded query cache: no fetch, no DOM, no new
 * dependency, and the data is there synchronously so the page renders its real content instead of
 * a spinner. Effects do not run, which is fine — everything asserted here is in the first paint.
 */

import { type QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { keys } from "../api/queries";
import { seedFailure } from "../api/queryTesting";
import type { Review } from "../api/types";
import { FOCUSED_STORAGE_KEY, TicketPage } from "./TicketPage";
import {
  CONFIG,
  type Entry,
  type IssueRead,
  run,
  seeded,
  TICKET_ID,
  type TicketOver,
} from "./TicketPage.fixture";

function render(
  extra: Entry[] = [],
  over: TicketOver = {},
  issueState?: IssueRead,
  config: Record<string, unknown> = CONFIG,
  prepare?: (qc: QueryClient) => void,
): string {
  const qc = seeded(extra, over, issueState, config);
  prepare?.(qc);
  return renderToString(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/tickets/${TICKET_ID}`]}>
        <Routes>
          <Route path="/tickets/:ticketId" element={<TicketPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The page in a browser that remembers Focused switched off: every row of the log. */
function renderAll(...args: Parameters<typeof render>): string {
  vi.stubGlobal("window", {
    localStorage: { getItem: (key: string) => (key === FOCUSED_STORAGE_KEY ? "0" : null) },
  });
  try {
    return render(...args);
  } finally {
    vi.unstubAllGlobals();
  }
}

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

/** SSR puts a comment marker between static text and an interpolation. */
const plain = (html: string) => html.replace(/<!-- -->/g, "");

/** The opening tag of the button whose text is `label`, or null when the page withholds it. An
 *  icon and the label's own wrapper sit between the two. */
function button(html: string, label: string): string | null {
  const found = html.match(new RegExp(`<button[^>]*>(?:<[^>]+>)*${label}<`));
  return found?.[0].match(/^<button[^>]*>/)?.[0] ?? null;
}

/** Every lozenge on the page, as its appearance and its word, in the order they are drawn. */
function lozenges(html: string): [string, string][] {
  return [
    ...html.matchAll(
      /data-appearance="(\w+)" style="max-width:[^"]*"[^>]*><span class="truncate">([^<]*)</g,
    ),
  ].map((found) => [found[1] as string, found[2] as string]);
}

const words = (html: string) => lozenges(html).map(([, word]) => word);

afterEach(() => {
  vi.useRealTimers();
});

describe("TicketPage", () => {
  it("renders the ticket header and the process graph", () => {
    const html = render();
    expect(plain(html)).toMatch(/<h1[^>]*data-page-title[^>]*><span[^>]*>#204 <span/);
    expect(words(html)).toContain("Waiting at gate");
    expect(html).toContain("flow-graph");
  });

  it("names the ticket by its issue title, and by its number alone when there is none", () => {
    // The stored observation, so this costs no GitHub read. A ticket that has not ticked since
    // titles were stored has none, and the header must not print an empty line for it.
    expect(render()).toContain("Remit-to address is dropped on re-issued invoices");
    expect(render([], { title: null })).not.toContain("Remit-to address");
  });

  it("cuts a long issue title short in its own grey, never the number", () => {
    // The ellipsis takes the style of the box that truncates. On the heading it would be the
    // number's bold black after grey words; on the title's own span it matches them.
    const heading = plain(render()).match(/<h1[^>]*>.*?<\/h1>/)?.[0] ?? "";
    expect(heading).toMatch(/#204 <span class="truncate[^"]*">Remit-to address is dropped/);
  });

  it("paints the gate the ticket is waiting on in the gate's own colour", () => {
    const html = render();
    // `spec_gate` is the current step, so its person glyph stops being a grey annotation and
    // takes the diamond's colour. It is the only gate in the config, so exactly one glyph does.
    expect(html).toContain("flow-node-person--current");
    expect(html.match(/flow-node-person--current/g)).toHaveLength(1);
    // Nothing has failed on it, so it stays the gate colour rather than the error one.
    expect(html).not.toContain("flow-node-person--error");
  });

  describe("the done node", () => {
    it("holds still on a ticket the loop has already marked done", () => {
      const html = render([], { status: "done", current_step: "done", step_runs: [] });
      expect(html).not.toContain("flow-node--current");
    });

    it("holds still in the tick before the loop marks it done", () => {
      // `current_step` reaches the terminal step one tick before `_terminal` writes DONE, so the
      // page can read a ticket standing on `done` while its status is still `running`. The node
      // is the end of the walk either way: nothing is live there and nothing wants a person.
      const html = render([], { status: "running", current_step: "done", step_runs: [] });
      expect(html).not.toContain("flow-node--current");
    });
  });

  describe("the running node", () => {
    it("keeps beating while its step runs", () => {
      const html = render([], {
        status: "running",
        current_step: "plan",
        step_runs: [run(1, "plan", 1, "running")],
      });
      expect(html).toContain("flow-node--running");
    });

    it("beats and then holds still at a gate, which is waiting rather than running", () => {
      const html = render();
      expect(html).toMatch(/flow-node--current(?!-)/);
      expect(html).not.toContain("flow-node--running");
    });
  });

  describe("a step entered but not started", () => {
    it("names what it is waiting on in the API's words, not the config's calls", () => {
      const html = render([], {
        current_step: "plan",
        status: "running",
        step_runs: [],
        waiting_on: ["an assignee on the issue"],
      });
      expect(html).toContain("Waiting for: an assignee on the issue");
      expect(html).not.toContain("Waiting for the condition");
      expect(html).not.toContain("ticket_has_assignee()");
    });

    it("names what it is waiting on from the pinned config when the API words nothing", () => {
      // On `plan` with no run rows: the shape between a promotion and the first spawn. The
      // fixture's ticket carries no `waiting_on`, as an API from before it sends none.
      const html = render([], { current_step: "plan", status: "running", step_runs: [] });
      expect(html).toContain("Waiting for the condition:");
      expect(html).toContain("ticket_has_assignee()");
    });

    it("says it once, on the newest attempt — the earlier one is history", () => {
      const html = render([], { current_step: "plan", status: "running", step_runs: [] });
      expect(html.split("Waiting for the condition:")).toHaveLength(2);
    });

    it("says nothing about a step whose run has already started", () => {
      // The SAME step and the same preconditions as the tests above — the only difference is
      // that its run rows are on the ticket, so its preconditions demonstrably held.
      const html = render([], { current_step: "plan", status: "running" });
      expect(html).not.toContain("Waiting for the condition");
    });

    it("marks the block starting, so the absent interrupt reads as not-yet", () => {
      const html = render([], { current_step: "plan", status: "running", step_runs: [] });
      expect(words(html)).toContain("Starting");
    });

    it("stops saying it once the run exists and the lozenge can speak for itself", () => {
      expect(words(render([], { current_step: "plan", status: "running" }))).not.toContain(
        "Starting",
      );
    });
  });

  describe("the runtime a step ran on", () => {
    const spawned: Entry = {
      id: 7,
      type: "agent_spawned",
      details: {
        step: "plan",
        attempt: 2,
        agent: "plan-agent",
        run_id: "204-plan-agent-bb09c1",
        runtime: "claude_cli",
        runtime_slug: "team-claude",
        runtime_type: "claude_subscription",
      },
      created_at: "2026-08-25T09:34:02Z",
    };

    it("links each run to the registry row it was spawned against", () => {
      const html = render([spawned]);
      expect(html).toMatch(/<a[^>]*href="\/runtimes\/team-claude"[^>]*>team-claude<\/a>/);
    });

    it("never shows the adapter kind, which is no runtime a reader can find", () => {
      // The fixture's event carries `runtime: claude_cli` and no slug — the shape that used to
      // print on the header.
      const html = render([spawned]);
      expect(html).not.toContain("claude_cli");
    });

    it("names the runtime the pinned config will start an agent step on", () => {
      const html = render(
        [],
        { current_step: "plan", status: "running", step_runs: [] },
        undefined,
        { ...CONFIG, runtime: "team-claude" },
      );
      expect(html).toContain("will run on");
      expect(html).toContain('href="/runtimes/team-claude"');
    });

    it("names none for a gate, which runs on no runtime", () => {
      const html = render(
        [
          {
            id: 7,
            type: "step_entered",
            details: { step: "spec_gate", attempt: 1, kind: "human_gate" },
            created_at: "2026-08-25T10:13:00Z",
          },
        ],
        { step_runs: [] },
        undefined,
        { ...CONFIG, runtime: "team-claude" },
      );
      expect(words(html)).toContain("Starting");
      expect(html).not.toContain("will run on");
    });
  });

  describe("a run a human interrupted", () => {
    // The situation this whole vocabulary was written for: the run is over, the ticket is parked
    // with `status = blocked`, and the three places that say so used to say three different
    // things — "blocked" in red overhead, an untouched blue node on the graph, and an amber
    // `interrupted` tag on the attempt below.
    const interrupted: TicketOver = {
      current_step: "plan",
      status: "blocked",
      step_runs: [
        run(1, "plan", 1, "failed"),
        run(2, "plan", 2, "interrupted", {
          failure_reason: "interrupted from the dashboard; the workspace is kept for a resume",
        }),
      ],
    };

    it("says Interrupted in the header, not Blocked", () => {
      const html = render([], interrupted);
      expect(words(html)).toContain("Interrupted");
      expect(words(html)).not.toContain("Blocked");
    });

    it("draws the header lozenge in the amber the attempt's own tag reads as", () => {
      const html = render([], interrupted);
      // Two lozenges say Interrupted — the ticket's and the attempt's — and both are the warning
      // role. The earlier `plan#1` failure keeps its danger tag: that attempt is history, and
      // history is not what the header is reporting.
      expect(lozenges(html).filter(([, word]) => word === "Interrupted")).toEqual([
        ["warning", "Interrupted"],
        ["warning", "Interrupted"],
      ]);
    });

    it("paints the step it is parked on amber, with its mark, on the ordinary beat", () => {
      const html = render([], interrupted);
      expect(html).toContain("flow-node--tone-attention");
      expect(html).toContain("flow-node-mark--attention");
      // Waiting on a person is not an alarm: it keeps the beat a live step has.
      expect(html).toContain("flow-node--current");
      expect(html).not.toContain("flow-node--tone-error");
    });
  });

  describe("a restarted ticket (#321)", () => {
    // Generation 1 ran `plan` and reached `spec_gate`, where the review was rejected; the restart
    // put the ticket back on `plan`, whose first run of generation 2 is attempt 2.
    const restarted: TicketOver = {
      generation: 2,
      status: "running",
      current_step: "plan",
      step_runs: [
        run(1, "plan", 1, "succeeded", { generation: 1 }),
        run(2, "spec_gate", 1, "rejected", { kind: "human_gate", generation: 1 }),
        run(3, "plan", 2, "running", { generation: 2 }),
      ],
    };

    it("draws the graph from this generation's runs: the gate it has not reached is unvisited", () => {
      const html = render([], restarted);
      expect(html).not.toContain("flow-node--tone-rejected");
      expect(html).not.toContain('aria-label="spec_gate: rejected');
    });

    it("offers no Retry for a run from before the restart, which the API would refuse", () => {
      const due = new Date(Date.now() + 5 * 60_000).toISOString();
      const html = render(
        [
          {
            id: 7,
            type: "retry_scheduled",
            details: { step: "plan", attempt: 1, next_attempt: 2, max_attempts: 3, due_at: due },
            created_at: "2026-08-25T10:20:00Z",
          },
        ],
        {
          generation: 2,
          status: "running",
          current_step: "plan",
          step_runs: [run(1, "plan", 1, "failed", { generation: 1 })],
        },
      );
      expect(button(html, "Retry")).toBeNull();
    });

    it("marks the new generation's attempt starting, not the one the old generation booked", () => {
      // The fixture's log booked `plan` #2 in generation 1; the restart entered #2 again.
      const html = renderAll(
        [
          {
            id: 7,
            type: "ticket_restarted",
            details: { step: "plan", attempt: null, generation: 2, requested_by: "teammate" },
            created_at: "2026-08-25T10:20:00Z",
          },
          {
            id: 8,
            type: "step_entered",
            details: { step: "plan", attempt: 2, reason: "restart" },
            created_at: "2026-08-25T10:20:00Z",
          },
        ],
        {
          generation: 2,
          status: "running",
          current_step: "plan",
          step_runs: [run(1, "plan", 1, "failed", { generation: 1 })],
        },
      );
      const blocks = html.split("<section").slice(1);
      const booking = blocks.find((block) => block.includes("Retry scheduled")) ?? "";
      const entered = blocks.find((block) => block.includes("Entered step")) ?? "";
      expect(words(booking)).not.toContain("Starting");
      expect(words(entered)).toContain("Starting");
    });
  });

  describe("exit and re-entry (decision 29)", () => {
    const exited: TicketOver = {
      status: "exited",
      current_step: "plan",
      step_runs: [run(1, "plan", 1, "cancelled", { failure_reason: "cancelled: ticket exited" })],
    };

    it("offers re-enter on an exited ticket, and nothing to interrupt", () => {
      const html = render([], exited, "open");
      expect(button(html, "Re-enter")).not.toBeNull();
      expect(button(html, "Exit")).toBeNull();
      expect(button(html, "Interrupt")).toBeNull();
    });

    it("offers exit on a live ticket", () => {
      expect(button(render(), "Exit")).not.toBeNull();
      expect(button(render(), "Re-enter")).toBeNull();
    });

    it("offers neither on a done ticket", () => {
      const html = render([], { status: "done", current_step: "done", step_runs: [] });
      expect(button(html, "Exit")).toBeNull();
      expect(button(html, "Re-enter")).toBeNull();
    });

    const reenterButton = (html: string) => button(html, "Re-enter");

    it("says it is checking the issue, rather than offering re-enter, until the read answers", () => {
      // A ticket that exited BECAUSE its issue closed is the likeliest one to be looked at, and
      // `closed` is false for as long as the read is in flight — so the first paint offered a
      // click the loop answers with a refusal comment.
      const html = render([], exited);
      expect(reenterButton(html)).toBeNull();
      expect(html).toContain("Checking the issue…");
    });

    it("offers re-enter once the read says the issue is open", () => {
      const tag = reenterButton(render([], exited, "open"));
      expect(tag).not.toBeNull();
      expect(tag).not.toMatch(/\sdisabled=""/);
      expect(tag).not.toContain("aria-disabled");
    });

    it("withholds re-enter while the issue is closed, and says in words how to bring it back", () => {
      // A disabled button leaves the tab order, and a browser shows no title on one, so the
      // reason lived where nobody could read it (#167). The loop would refuse the request.
      const html = render([], exited, "closed");
      expect(reenterButton(html)).toBeNull();
      expect(html.replace(/<!-- -->/g, "")).toContain(
        "Issue #204 is closed on GitHub. Reopen it to re-enter.",
      );
      expect(html).toMatch(
        /<a[^>]*href="https:\/\/github\.com\/northwind\/harbor-invoices\/issues\/204"[^>]*>Open issue(?:<[^>]+>)*<\/a>/,
      );
    });

    it("still offers re-enter when the issue read failed, and lets the loop decide", () => {
      const html = render([], exited, { error: "This project has no GitHub connection" });
      expect(reenterButton(html)).not.toBeNull();
      expect(html).not.toContain("Checking the issue");
    });

    // Both buttons arm in place rather than confirming through a modal, so first paint — all
    // `renderToString` can show us — must still be the plain button: the confirm strip is
    // conditionally MOUNTED, not merely hidden, so a bug that rendered it by default would show
    // up here even though nothing here can click.
    it("mounts no confirm step for exit before anything is clicked", () => {
      const html = render();
      expect(button(html, "Exit")).toContain('aria-expanded="false"');
      expect(html).not.toContain("Any running work stops");
      expect(button(html, "Cancel")).toBeNull();
    });

    it("mounts no confirm step for re-enter before anything is clicked", () => {
      const html = render([], exited, "open");
      expect(button(html, "Re-enter")).toContain('aria-expanded="false"');
      expect(html).not.toContain("after a short wait");
      expect(button(html, "Cancel")).toBeNull();
    });
  });

  describe("quarantine (decision 31)", () => {
    const quarantined: TicketOver = { quarantined_at: "2026-08-25T11:00:00Z" };

    it("says quarantined in the header even though the ticket is otherwise waiting at a gate", () => {
      // Quarantine is orthogonal to status: this fixture's status is still `waiting_gate`, and
      // the pill must say quarantined anyway.
      const html = render([], quarantined);
      expect(lozenges(html)).toContainEqual(["danger", "Stopped after errors"]);
      expect(words(html)).not.toContain("Waiting at gate");
    });

    it("says above the page what quarantine means and the way out of it", () => {
      const html = render([], quarantined);
      expect(html).toMatch(/<section[^>]*data-appearance="error"/);
      expect(html).toContain(
        "Find the latest error in Activity, fix it, then press Requeue ticket.",
      );
      expect(render()).not.toContain("Find the latest error in Activity");
    });

    it("offers requeue only on a quarantined ticket", () => {
      expect(button(render([], quarantined), "Requeue")).not.toBeNull();
      expect(button(render(), "Requeue")).toBeNull();
    });

    it("offers no exit while quarantined, because nothing would read the request", () => {
      // `acquire_leases` skips a quarantined ticket, so an `exit_requested` event filed here is
      // never observed. The button must only offer what the loop will actually act on.
      const html = render([], quarantined);
      expect(button(html, "Requeue")).not.toBeNull();
      expect(button(html, "Exit")).toBeNull();
    });

    it("offers no interrupt while quarantined, though the run is still RUNNING", () => {
      // The reachable case: quarantine leaves `status` and the `step_run` row untouched, so a
      // quarantined ticket routinely still has a live agent run and the panel would draw its
      // stop button. `acquire_leases` never hands the ticket back, so the request would never
      // be read — the route 409s it, and the page must not offer it either.
      const live: TicketOver = {
        current_step: "plan",
        status: "running",
        step_runs: [run(1, "plan", 1, "running")],
      };
      expect(button(render([], live), "Interrupt")).not.toBeNull();
      expect(button(render([], { ...live, ...quarantined }), "Interrupt")).toBeNull();
    });

    it("mounts no confirm step for requeue before anything is clicked", () => {
      const html = render([], quarantined);
      expect(button(html, "Requeue")).toContain('aria-expanded="false"');
      expect(html).not.toContain("Factorial tries again");
      expect(button(html, "Cancel")).toBeNull();
    });

    it("renders the quarantine event with its error text", () => {
      const html = render([
        {
          id: 8,
          type: "ticket_quarantined",
          details: { error: "GitHub API error: 403 archived repository", failures: 5 },
          created_at: "2026-08-25T11:00:00Z",
        },
      ]);
      expect(html).toContain("Stopped after errors");
      expect(html).toContain("archived repository");
    });
  });

  describe("refresh (decision 4)", () => {
    const refreshButton = (html: string) => button(html, "Refresh");

    it("offers refresh on a live ticket, with nothing to confirm", () => {
      const tag = refreshButton(render());
      expect(tag).not.toBeNull();
      expect(tag).not.toContain('disabled=""');
      // Not an armed action: a pass costs nothing a reader has to be warned about.
      expect(tag).not.toContain("aria-expanded");
    });

    it("offers no refresh where no pass would run", () => {
      // Done and quarantined tickets are never leased, so the route refuses them.
      expect(refreshButton(render([], { status: "done", current_step: "done" }))).toBeNull();
      expect(refreshButton(render([], { quarantined_at: "2026-08-25T11:00:00Z" }))).toBeNull();
    });

    it("withholds refresh while the ticket's passes are failing, and says why in words", () => {
      // Each pass there counts toward quarantine (decision 31), so the route refuses it; the loop
      // retries it anyway. The reason is text beside the header, not a title on a dead button.
      const html = render([], { consecutive_failures: 2 });
      expect(refreshButton(html)).toBeNull();
      expect(html).toContain(
        "Factorial couldn&#x27;t read this ticket&#x27;s latest state. It keeps trying, and you don&#x27;t need to do anything yet.",
      );
    });
  });

  describe("a read that fails", () => {
    it("keeps the ticket on screen when a background refetch fails, and says it is stale", () => {
      // The ticket polls every 10 s, so one network blip used to swap the whole page for a red
      // line. TanStack keeps the last data through a failed refetch; the page has to as well.
      const html = render([], {}, undefined, CONFIG, (qc) =>
        seedFailure(
          qc,
          keys.ticket(TICKET_ID),
          "cannot reach the API",
          qc.getQueryData(keys.ticket(TICKET_ID)),
        ),
      );
      expect(html).toContain("Remit-to address is dropped on re-issued invoices");
      expect(html).toContain("flow-graph");
      expect(html).toContain("Couldn&#x27;t refresh this page. Showing data from");
      expect(html).toContain("Retrying.");
    });

    it("shows the error in place of the page only when nothing has loaded yet", () => {
      const html = render([], {}, undefined, CONFIG, (qc) => {
        qc.removeQueries({ queryKey: keys.ticket(TICKET_ID), exact: true });
        seedFailure(qc, keys.ticket(TICKET_ID), "cannot reach the API");
      });
      expect(html).toContain("cannot reach the API");
      expect(html).not.toContain("flow-graph");
    });
  });

  it("disables no header control with only a tooltip to say why", () => {
    for (const html of [
      render(),
      render([], { consecutive_failures: 2 }),
      render([], { status: "exited", current_step: "plan", step_runs: [] }),
      render([], { status: "exited", current_step: "plan", step_runs: [] }, "closed"),
    ]) {
      expect(html).not.toMatch(/<button(?=[^>]*\sdisabled="")(?=[^>]*\stitle=)[^>]*>/);
    }
  });

  it("keeps an escalation red on every surface", () => {
    const html = render([], {
      current_step: "plan",
      status: "blocked",
      step_runs: [run(1, "plan", 1, "failed")],
    });
    expect(html).toContain("flow-node--tone-error");
    // Said by its red and its mark, never flashed: a failed step does not take the beat.
    expect(html).toContain("flow-node-mark--error");
    expect(html).not.toMatch(/flow-node--current(?!-)/);
    // And in words, for anyone who cannot see either.
    expect(html).toContain('aria-label="plan: failed, 1 attempt, current step"');
    expect(lozenges(html).filter(([, word]) => word === "Blocked")).toEqual([
      ["danger", "Blocked"],
    ]);
    expect(html).not.toContain("flow-node--tone-attention");
  });

  it("puts the ticket's fixed facts in the header's bottom bar, not in a list above the graph", () => {
    const html = render();
    const header = html.slice(0, html.indexOf("</header>"));
    expect(header).toContain("operator");
    // The lane and the commit its config was read from, not the snapshot row's primary key —
    // that id names nothing a reader can look up, and the sha names a commit on GitHub.
    expect(header).toContain("standard");
    expect(header).toContain("a1b2c3d");
    expect(html).not.toContain("#17");
    expect(html).not.toContain("<dl");
  });

  it("counts down to a retry that has not come due, and reports one that has", () => {
    // The whole point of the row while a backoff runs: the ticket looks idle, and this is the
    // only thing on the page that says something is still coming.
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-25T09:30:00Z"));
    const pending = render([
      {
        id: 9,
        type: "retry_scheduled",
        details: { step: "build", attempt: 1, next_attempt: 2, due_at: "2026-08-25T09:34:18Z" },
        created_at: "2026-08-25T09:24:18Z",
      },
    ]);
    expect(plain(pending)).toContain("in 4m 18s");
    // When, exactly, is behind the icon beside it — see the happy-dom test for its wording.
    expect(pending).toContain('aria-label="When the retry runs"');

    // Past due, the clock stops: the worker starts the attempt on its next tick, and a countdown
    // running into negative numbers would claim to know something the page cannot.
    vi.setSystemTime(new Date("2026-08-25T11:00:00Z"));
    const elapsed = plain(render());
    expect(elapsed).not.toContain("4m 18s");
    expect(elapsed).toMatch(/due [^<]*ago/);
  });

  it("groups events under attempts, with a rail entry per block", () => {
    const html = render();
    expect(words(plain(html))).toContain("Attempt 2");
    expect(html).toContain("Expected results missing");
    expect(html).toContain("Retry scheduled");
    expect(html).toContain("Agent completed");
  });

  it("shows a run whose timeline events have not arrived yet", () => {
    const html = render();
    expect(html).toContain("spec_gate");
    expect(html).toContain("No events recorded for this attempt yet.");
  });

  describe("an agent at work", () => {
    // Attempt 2 of `plan` is running: the ticket's own state, the run's, and nothing else.
    const working: TicketOver = {
      current_step: "plan",
      status: "running",
      step_runs: [
        run(1, "plan", 1, "failed", { runtime_run_id: "204-plan-agent-7f3a2c" }),
        run(2, "plan", 2, "running", { runtime_run_id: "204-plan-agent-bb09c1" }),
      ],
    };

    it("is said at the live end of its step while the ticket runs", () => {
      expect(render([], working)).toContain("Agent working");
    });

    it("is not said once the ticket stopped after errors, though its run still reads running", () => {
      const quarantined = { ...working, quarantined_at: "2026-08-25T10:20:00Z" };
      expect(render([], quarantined)).not.toContain("Agent working");
    });

    it("is not said while the agent waits on a person", () => {
      const idle = {
        ...working,
        step_runs: [run(2, "plan", 2, "idle", { runtime_run_id: "204-plan-agent-bb09c1" })],
      };
      expect(render([], idle)).not.toContain("Agent working");
    });
  });

  it("folds unbounded content behind buttons instead of inlining it", () => {
    const html = render();
    expect(button(html, "Terminal")).not.toBeNull();
    // The tail of the long summary must not be in the row markup.
    expect(html).not.toContain("Needs a decision");
    // The step header does not repeat a failed run's reason: the event row that reports the
    // failure carries the same text, and its own way into the full version.
    expect(html).not.toContain("why it failed");
  });

  it("reads newest first, blocks and events alike", () => {
    const html = renderAll();
    // `spec_gate` has no events yet, which puts it at the newest end of the log.
    expect(html.indexOf("No events recorded")).toBeLessThan(
      html.indexOf("Expected results missing"),
    );
    // And inside a block, the last thing that happened is the first thing shown.
    expect(html.indexOf("Expected results missing")).toBeLessThan(html.indexOf("Agent started"));
    expect(html.indexOf("Agent started")).toBeLessThan(html.indexOf("Step started"));
  });

  it("sorts the activity with one icon beside its title, named for what a press does", () => {
    const html = render();
    const header = html.slice(html.indexOf(">Activity<"));
    const sort = header.match(/<button[^>]*aria-label="Show oldest first"[^>]*>.*?<\/button>/)?.[0];
    // The glyph is the order the log is in now: newest at the top, so sorted descending.
    expect(sort).toContain('data-glyph="sort-down"');
    // Outside the heading, so the panel is still named Activity alone.
    expect(html).toMatch(/<h2[^>]*>(?:<[^>]+>)*Activity(?:<[^>]+>)*<\/h2>/);
    expect(html).not.toContain("Newest first");
    expect(html).not.toContain('aria-label="Order"');
  });

  describe("where a message goes", () => {
    /** The activity block whose header names `step`: from its header to the next block's. */
    const block = (html: string, step: string) => {
      const activity = html.slice(html.indexOf(">Activity<"));
      const start = activity.indexOf(`>${step}</code>`);
      const next = activity.indexOf('<header class="sticky', start);
      return activity.slice(start, next === -1 ? undefined : next);
    };

    it("puts the gate's message in the gate's block, under its header", () => {
      const html = render();
      const gate = block(html, "spec_gate");
      expect(gate.indexOf("</header>")).toBeGreaterThan(-1);
      expect(gate.indexOf("</header>")).toBeLessThan(gate.indexOf("Waiting for a review"));
      // Said once, and not over the graph.
      expect(html.match(/Waiting for a review/g)).toHaveLength(1);
      expect(html.indexOf("Waiting for a review")).toBeGreaterThan(html.indexOf("flow-graph"));
    });

    it("keeps a message about the whole ticket at the top", () => {
      const html = render([], { quarantined_at: "2026-08-25T11:00:00Z" });
      expect(html.indexOf("Stopped after repeated errors")).toBeLessThan(
        html.indexOf("flow-graph"),
      );
    });

    it("keeps the gate's message at the top until the log can draw the gate's block", () => {
      const html = render([], {}, undefined, CONFIG, (qc) =>
        qc.removeQueries({ queryKey: keys.timeline(TICKET_ID) }),
      );
      expect(html).toContain("Loading the activity");
      expect(html).toContain("Waiting for a review");
      expect(html.indexOf("Waiting for a review")).toBeLessThan(html.indexOf("flow-graph"));
    });
  });

  describe("Focused", () => {
    /** The switch's opening tag, found by its label. */
    const focusedSwitch = (html: string) =>
      html.match(/<button[^>]*role="switch"[^>]*aria-labelledby="([^"]+)"[^>]*>/g)?.find((tag) => {
        const id = tag.match(/aria-labelledby="([^"]+)"/)?.[1];
        return id !== undefined && html.includes(`id="${id}">Focused<`);
      });

    it("is on for a reader who never switched it, leaving the routine rows out", () => {
      const html = render();
      expect(focusedSwitch(html)).toContain('aria-checked="true"');
      expect(html).toContain("Expected results missing");
      expect(html).toContain("Retry scheduled");
      expect(html).not.toContain("Agent started");
      expect(html).not.toContain("Step started");
    });

    it("shows every row to a reader whose browser remembers it off", () => {
      const html = renderAll();
      expect(focusedSwitch(html)).toContain('aria-checked="false"');
      expect(html).toContain("Agent started");
      expect(html).toContain("Step started");
    });
  });

  it("totals the ticket's spend in the Activity header", () => {
    // Every figure is boxed with the unit that measures it — the reader compares the numbers down
    // the column, and the units only say how to read them.
    expect(metrics(render())).toEqual(
      expect.arrayContaining([
        ["41.9K", "in"],
        ["9.2K", "out"],
        ["$", "0.94"],
        // 19m18s on the first attempt plus 38m33s on the second; the gap between them is not spend.
        ["57", "m"],
        ["51", "s"],
      ]),
    );
  });

  it("marks the header total as an estimate when any attempt's figure is one", () => {
    // A codex attempt is priced at list, not billed (decision 34). One such part makes the
    // ticket's whole sum an estimate, and the tilde rides ahead of the box rather than in it.
    const html = render([], {
      step_runs: [
        run(1, "plan", 1, "failed", { cost_usd: "0.2000", cost_source: "reported" }),
        run(2, "plan", 2, "succeeded", {
          model: "gpt-5.3-codex",
          cost_usd: "0.7441",
          cost_source: "estimated",
          input_tokens: 40000,
          cached_input_tokens: 30000,
          output_tokens: 9000,
        }),
      ],
    });
    expect(metrics(html)).toEqual(expect.arrayContaining([["$", "0.94"]]));
    // Captioned in words rather than in a native tooltip, with the tilde ahead of the box.
    expect(html).toMatch(/>Cost<\/span><span>~<\/span><span data-metric-box/);
    expect(html).toContain('aria-label="Why the cost is an estimate"');
  });

  it("draws a dash, not a free ticket, when no attempt was priced", () => {
    const html = render([], {
      step_runs: [run(1, "plan", 1, "succeeded", { input_tokens: 41880, output_tokens: 9204 })],
    });
    expect(metrics(html)).toEqual(expect.arrayContaining([["41.9K", "in"]]));
    expect(metrics(html)).not.toEqual(expect.arrayContaining([["$", "0.00"]]));
    expect(html).toMatch(/>Cost<\/span><span>-<\/span>/);
    expect(html).not.toContain("Cost (est.)");
    expect(html).not.toContain("Why the cost is an estimate");
  });

  it("offers the issue's description and the issue itself, not one bare link", () => {
    const html = render();
    // The description opens in a drawer; "Issue" is a real anchor at the issue, so
    // cmd-click and "open in new tab" still work. Short words, named in full.
    expect(button(html, "Description")).toContain('aria-label="Description of #204"');
    expect(html).toMatch(
      /<a[^>]*href="https:\/\/github\.com\/northwind\/harbor-invoices\/issues\/204"[^>]*aria-label="Issue #204 on GitHub"[^>]*>(?:<[^>]+>)*Issue</,
    );
    expect(html).not.toContain("Open issue");
    // Nothing has been clicked, so the description has not been fetched and no panel exists.
    expect(html).not.toContain('role="dialog"');
    expect(html).not.toContain("Reading the issue from GitHub");
  });

  it("gives a row that names a GitHub thing a link to it", () => {
    const html = renderAll();
    expect(html).toContain(
      'href="https://github.com/northwind/harbor-invoices/tree/spec/204-remit-to"',
    );
  });

  it("blinks the link on the row the waiting ticket is stopped at", () => {
    // The whole chain: the ticket's own `waiting_gate` on `spec_gate` decides which link moves,
    // so a page that stopped passing the hold down would leave this one still.
    const html = renderAll([
      {
        id: 7,
        type: "gate_waiting",
        details: { step: "spec_gate", attempt: 1, pr_number: 248 },
        created_at: "2026-08-25T10:13:02Z",
      },
    ]);
    // The activity's rows only: the gate's message, in the gate's block, links the same pull
    // request and holds still — it is the way to act, not the row that says where it stopped.
    const activity = html
      .slice(html.indexOf(">Activity<"))
      .split("<li")
      .slice(1)
      .map((part) => part.slice(0, part.indexOf("</li>")))
      .join("");
    const [pr] = (activity.match(/<a [^>]*>/g) ?? []).filter((tag) => tag.includes("/pull/248"));
    expect(pr).toContain("flow-blink");
    // Its neighbour in the previous block names a branch and says nothing about what to do next.
    const [branch] = (activity.match(/<a [^>]*>/g) ?? []).filter((tag) =>
      tag.includes("/tree/spec/204-remit-to"),
    );
    expect(branch).not.toContain("flow-blink");
  });

  it("says what a waiting gate needs, and links the pull request it is waiting on", () => {
    const gate: Entry = {
      id: 7,
      type: "gate_waiting",
      details: { step: "spec_gate", attempt: 1, pr_number: 248 },
      created_at: "2026-08-25T10:13:02Z",
    };
    const html = render([gate]);
    expect(html).toContain(
      "Pull request #248 needs a person&#x27;s decision before the ticket moves on.",
    );
    expect(html).toMatch(
      /<a[^>]*href="https:\/\/github\.com\/northwind\/harbor-invoices\/pull\/248"[^>]*>View pull request(?:<[^>]+>)*<\/a>/,
    );
    // Without a pull request on the log, it still says what the step is waiting for.
    expect(render()).toContain(
      "This step needs a person&#x27;s decision before the ticket moves on.",
    );
    expect(render()).not.toContain("View pull request");
  });

  describe("a parked run of a step that awaits review", () => {
    const REVIEWED = {
      ...CONFIG,
      steps: CONFIG.steps.map((step) =>
        step.name === "plan" ? { ...step, postconditions: "request_approval()" } : step,
      ),
    };
    const parked: TicketOver = {
      current_step: "plan",
      status: "blocked",
      park_reason: "idle",
      step_runs: [run(1, "plan", 1, "interrupted")],
    };
    const reviewed = (over: Partial<Review>) => (qc: QueryClient) =>
      qc.setQueryData(keys.runReview(1), {
        awaits_review: true,
        branch: "plan/204-remit",
        head_sha: "4f1c2a9e0b7d",
        pushed: true,
        approval: null,
        offered: true,
        refusal: null,
        ...over,
      } satisfies Review);
    const nothingPushed = reviewed({
      branch: null,
      head_sha: null,
      pushed: false,
      refusal: "Nothing is pushed yet.",
    });

    it("asks for a review once the run pushed a commit to approve", () => {
      const html = plain(render([], parked, undefined, REVIEWED, reviewed({})));
      expect(html).toContain("Waiting for your review");
      expect(html).not.toContain("The agent waited for an answer");
    });

    it("says the agent waited for an answer when it asked before pushing anything", () => {
      const html = plain(render([], parked, undefined, REVIEWED, nothingPushed));
      expect(html).toContain("The agent waited for an answer and nobody answered");
      expect(html).not.toContain("Waiting for your review");
    });

    it("is not a review when the head is an earlier step's on a shared branch", () => {
      const html = plain(render([], parked, undefined, REVIEWED, reviewed({ pushed: false })));
      expect(html).toContain("The agent waited for an answer and nobody answered");
      expect(html).not.toContain("Waiting for your review");
    });

    it("is not a review while Approve would be refused", () => {
      const refused = reviewed({ refusal: "A Resume is already waiting for this run." });
      const html = plain(render([], parked, undefined, REVIEWED, refused));
      expect(html).not.toContain("Waiting for your review");
    });
  });
});
