// @vitest-environment happy-dom
/**
 * The activity region's markup, rendered on its own.
 *
 * What a row's link looks like on the attempt the ticket is stopped at, and what it looks like
 * everywhere else — including on the earlier attempts of that same step, which are history and
 * ask nothing of anyone. `TicketPage.test.tsx` renders this component through the page, but its
 * fixture holds a gate whose events have not arrived yet, so the shapes these rules are about are
 * built here directly. Mostly server-rendered; the one explanation that only opens on a press is
 * mounted (decision 51).
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { api } from "../../api/client";
import type { StepRun, TimelineEntry } from "../../api/types";
import { run as fixtureRun } from "../../pages/TicketPage.fixture";
import { ROLE_BOLD, ROLE_SUBTLE, ROLE_TEXT } from "../../status";
import { byText, click, mount } from "../../testDom";
import { ActivityPanel, type Waiting } from "./ActivityPanel";
import { type ActivityOrder, type ActivitySegment, type Held, segmentsFrom } from "./activity";
import type { RetryOffer } from "./retry";

const REPO = "northwind/harbor-invoices";

function ev(id: number, type: string, details: Record<string, unknown>): TimelineEntry {
  return { id, type, details, created_at: "2026-08-25T09:00:00Z" };
}

function run(
  step_name: string,
  attempt: number,
  status: StepRun["status"],
  kind: StepRun["kind"],
): StepRun {
  return fixtureRun(attempt, step_name, attempt, status, { kind }) as StepRun;
}

function panel(
  entries: TimelineEntry[],
  runs: StepRun[],
  held: Held | null,
  waitingOn?: (segment: ActivitySegment) => Waiting | null,
  startingKey?: string | null,
  outputOpener?: (segment: ActivitySegment) => ((run: StepRun) => void) | undefined,
  retry?: RetryOffer,
) {
  return (
    <ActivityPanel
      retry={retry}
      segments={segmentsFrom(entries, runs)}
      order="oldest"
      repo={REPO}
      activeKey={null}
      onActiveKeyChange={() => {}}
      runtimeFor={() => undefined}
      onOpenText={() => {}}
      outputOpener={outputOpener}
      held={held}
      waitingOn={waitingOn}
      startingKey={startingKey}
    />
  );
}

function render(...args: Parameters<typeof panel>): string {
  return renderToString(panel(...args));
}

/** Every anchor whose href contains `fragment`, as raw opening tags. */
function anchors(html: string, fragment: string): string[] {
  const found = (html.match(/<a [^>]*>/g) ?? []).filter((tag) => tag.includes(fragment));
  expect(found.length).toBeGreaterThan(0);
  return found;
}

/** The one `<li>` whose markup contains `label` — the rows alone, not the header above them,
 *  whose status lozenge can carry the same word. */
function row(html: string, label: string): string {
  const found = html
    .split("<li")
    .slice(1)
    .map((part) => part.slice(0, part.indexOf("</li>")))
    .filter((part) => part.includes(label));
  expect(found).toHaveLength(1);
  return found[0] as string;
}

/** A row's label, and whether it is set in the default ink or the subtle one. */
const labelInk = (html: string, label: string) =>
  row(html, label).match(
    new RegExp(`class="[^"]*\\b(text-default|text-subtle)\\b[^"]*">${label}<`),
  )?.[1];

/**
 * A gate that was rejected once and is waiting again: attempt 1 is finished history, attempt 2 is
 * where the ticket is stopped. Both attempts name a PR.
 */
const GATE = [
  ev(1, "step_started", { step: "spec_gate", attempt: 1, kind: "human_gate" }),
  ev(2, "gate_waiting", { step: "spec_gate", attempt: 1, pr_number: 240 }),
  ev(3, "gate_rejected", { step: "spec_gate", attempt: 1, pr_number: 240, reason: "closed" }),
  ev(4, "step_started", { step: "spec_gate", attempt: 2, kind: "human_gate" }),
  ev(5, "gate_waiting", { step: "spec_gate", attempt: 2, pr_number: 248 }),
];
const GATE_RUNS = [
  run("spec_gate", 1, "rejected", "human_gate"),
  run("spec_gate", 2, "waiting", "human_gate"),
];
const HELD: Held = { key: "spec_gate#2", condition: "waiting_gate" };

describe("ActivityPanel's run header cost", () => {
  const AGENT = [ev(1, "step_started", { step: "build", attempt: 1, kind: "agent" })];

  const ESTIMATED: StepRun[] = [
    { ...run("build", 1, "succeeded", "agent"), cost_usd: "0.2559", cost_source: "estimated" },
  ];

  it("labels a list-price estimate and leaves a measured figure alone", () => {
    const estimated = render(AGENT, ESTIMATED, null);
    // Captioned as an estimate for a reader, `~` ahead of the box, and a way into why it is one — never a native `title`, which a keyboard never shows.
    expect(estimated).toMatch(
      /Cost, estimated(?:<!-- -->)? <\/span><span>~<\/span><span data-metric-box/,
    );
    expect(estimated).toContain('aria-label="Why this cost is an estimate"');
    expect(estimated).not.toContain("title=");

    const reported = render(
      AGENT,
      [{ ...run("build", 1, "succeeded", "agent"), cost_usd: "0.2559", cost_source: "reported" }],
      null,
    );
    expect(reported).toMatch(/>Cost(?:<!-- -->)? <\/span><span data-metric-box/);
    expect(reported).not.toContain("~");
    expect(reported).not.toContain("Why this cost is an estimate");
  });

  it("says why a figure is an estimate, when its mark is pressed", async () => {
    const view = await mount(panel(AGENT, ESTIMATED, null));
    expect(document.body.textContent).not.toContain("list rates");
    await click(view.container.querySelector('[aria-label="Why this cost is an estimate"]'));
    const opened = document.querySelector('[role="dialog"]');
    expect(opened?.textContent).toContain("Estimated cost");
    expect(opened?.textContent).toContain("list rates");
    await view.unmount();
  });

  it("says a live run's figure is so far, and drops it once the end's figure lands", () => {
    const live: StepRun = {
      ...run("build", 1, "running", "agent"),
      input_tokens: 1_000_000,
      output_tokens: 100_000,
      cost_usd: "1.1650",
      cost_source: "estimated",
    };
    const going = render(AGENT, [live], null);
    expect(going).toMatch(/Cost so far, estimated(?:<!-- -->)? <\/span><span>~<\/span>/);
    expect(going).toContain(">so far<");
    expect(going).toContain("Tokens so far");

    const ended = render(
      AGENT,
      [{ ...live, status: "succeeded", cost_usd: "1.5000", cost_source: "reported" }],
      null,
    );
    expect(ended).not.toContain("so far");
    expect(ended).toMatch(/>Cost(?:<!-- -->)? <\/span><span data-metric-box/);
  });
});

describe("ActivityPanel's run header output", () => {
  const AGENT = [ev(1, "step_started", { step: "build", attempt: 1, kind: "agent" })];
  const RUNS = [run("build", 1, "succeeded", "agent")];

  it("opens a run's transcript, preview and terminal from one Output button", async () => {
    const opened: number[] = [];
    const view = await mount(
      panel(AGENT, RUNS, null, undefined, undefined, () => (shown) => opened.push(shown.id)),
    );
    const header = view.container.querySelector("header") as HTMLElement;
    expect([...header.querySelectorAll("button")].map((b) => b.textContent?.trim())).toEqual([
      "Output",
    ]);
    await click(byText(header, "button", "Output"));
    expect(opened).toEqual([1]);
    await view.unmount();
  });

  it("offers no Output on a run that has none", () => {
    expect(render(AGENT, RUNS, null)).not.toContain(">Output<");
  });
});

describe("ActivityPanel's Retry", () => {
  const inMs = (ms: number) => new Date(Date.now() + ms).toISOString();
  const failed = { ...run("plan", 1, "failed", "agent"), id: 7 };
  const entries = (due: string) => [
    ev(1, "step_started", { step: "plan", attempt: 1, kind: "agent" }),
    ev(2, "agent_failed", { step: "plan", attempt: 1, reason: "agent gave up" }),
    ev(3, "retry_scheduled", { step: "plan", attempt: 1, next_attempt: 2, due_at: due }),
  ];

  async function shown(due: string, offered = true) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return mount(
      <QueryClientProvider client={qc}>
        {panel(
          entries(due),
          [failed],
          null,
          undefined,
          undefined,
          undefined,
          offered ? { run: failed, due, ticketId: 9 } : undefined,
        )}
      </QueryClientProvider>,
    );
  }

  /** The header of the block whose rows include `row`. */
  const header = (root: HTMLElement, row: string) =>
    [...root.querySelectorAll("section")]
      .find((block) => block.querySelector("ol")?.textContent?.includes(row))
      ?.querySelector("header") as HTMLElement;
  const popup = () =>
    document.querySelector<HTMLElement>('[role="dialog"][aria-label="Retry plan?"]');

  it("sits in the header of the failed attempt, and starts the next once confirmed", async () => {
    const retry = vi
      .spyOn(api, "retryStep")
      .mockResolvedValue({ status: "requested", landed: true });
    const view = await shown(inMs(5 * 60_000));
    // The block the backoff's countdown is filed under is not the run a Retry follows.
    expect(byText(header(view.container, "Retry scheduled"), "button", "Retry")).toBeNull();
    for (const rows of view.container.querySelectorAll<HTMLElement>("ol")) {
      expect(byText(rows, "button", "Retry")).toBeNull();
    }

    await click(byText(header(view.container, "Agent failed"), "button", "Retry"));
    // Anchored to its button, over the page, like Exit and Interrupt: not a modal.
    expect(popup()?.getAttribute("aria-modal")).not.toBe("true");
    expect(popup()?.textContent).toContain("Attempt 2 continues attempt 1's session");
    expect(retry).not.toHaveBeenCalled();

    await click(byText(popup() as HTMLElement, "button", "Retry step"));
    expect(retry).toHaveBeenCalledWith(7);
    expect(popup()).toBeNull();
    await view.unmount();
    retry.mockRestore();
  });

  it("closes on Cancel and sends nothing", async () => {
    const retry = vi.spyOn(api, "retryStep");
    const view = await shown(inMs(5 * 60_000));

    await click(byText(header(view.container, "Agent failed"), "button", "Retry"));
    await click(byText(popup() as HTMLElement, "button", "Cancel"));
    expect(popup()).toBeNull();
    expect(retry).not.toHaveBeenCalled();
    await view.unmount();
    retry.mockRestore();
  });

  it("is offered only where the caller offers it", async () => {
    const elsewhere = await shown(inMs(5 * 60_000), false);
    expect(byText(elsewhere.container, "button", "Retry")).toBeNull();
    await elsewhere.unmount();
  });

  it("stops the countdown, and says it was cancelled, once a person retried the run early", () => {
    const due = inMs(5 * 60_000);
    const html = render(
      [
        ...entries(due),
        ev(4, "retry_requested", {
          step: "plan",
          attempt: 1,
          next_attempt: 2,
          requested_by: "teammate",
        }),
        ev(5, "step_started", { step: "plan", attempt: 2, kind: "agent" }),
      ],
      [failed, { ...run("plan", 2, "running", "agent"), id: 8 }],
      null,
    );
    const scheduled = row(html, "Retry scheduled");
    expect(scheduled).toContain("Canceled: retried now by teammate");
    expect(scheduled).not.toContain("When the retry runs");
    expect(scheduled).not.toContain('aria-label="Needs attention"');
  });
});

describe("ActivityPanel on a step entered but not started", () => {
  /** One entry and no run: the shape a step has between promotion and its first spawn. */
  const ENTERED = [ev(1, "step_entered", { step: "build", attempt: 1, kind: "agent" })];

  const calls =
    (...called: string[]) =>
    () => ({ needs: [], calls: called });

  it("names the conditions in words when the API words them, and not the calls", () => {
    const html = render(ENTERED, [], null, () => ({
      needs: ["the pull request from spec/204-… merged into main", "an assignee on the issue"],
      calls: ['pr_merged("spec/204-*", "main")', "ticket_has_assignee()"],
    }));
    expect(html).toContain(
      "Waiting for: the pull request from spec/204-… merged into main; an assignee on the issue",
    );
    expect(html).not.toContain("Waiting for the condition");
    expect(html).not.toContain("pr_merged");
  });

  it("names the conditions the caller resolved from the pinned config, without words", () => {
    const html = render(ENTERED, [], null, calls('pr_merged("spec/204-*", "main")'));
    expect(html).toContain("Waiting for the condition:");
    // The call as config wrote it, less React's entity-escaping of its quotes.
    expect(html).toContain("pr_merged(&quot;spec/204-*&quot;, &quot;main&quot;)");
  });

  it("joins several conditions rather than claiming which one is false", () => {
    const html = render(ENTERED, [], null, calls("a()", "b()"));
    expect(html).toContain("a(), b()");
  });

  it("marks that block starting, so an absent interrupt reads as not-yet rather than missing", () => {
    expect(render(ENTERED, [], null, undefined, "build#1")).toContain(">Starting<");
  });

  it("marks it only on the block the ticket is actually on", () => {
    expect(render(ENTERED, [], null, undefined, null)).not.toContain(">Starting<");
    expect(render(ENTERED, [], null, undefined, "build#2")).not.toContain(">Starting<");
  });

  it("says nothing when the caller has nothing to say", () => {
    for (const html of [
      render(ENTERED, [], null, () => null),
      render(ENTERED, [], null, calls()),
      render(ENTERED, [], null),
    ]) {
      expect(html).not.toContain("Waiting for the condition");
      expect(html).not.toContain("Waiting for:");
    }
  });
});

describe("ActivityPanel row links", () => {
  it("sends the reader to the pull request the gate is waiting on", () => {
    const html = render(GATE, GATE_RUNS, HELD);
    expect(anchors(html, "/pull/248")[0]).toContain(`href="https://github.com/${REPO}/pull/248"`);
  });

  it("shows an unresolved branch template as plain text, with no anchor to a missing ref", () => {
    const html = render(
      [
        ev(1, "step_started", { step: "build", attempt: 1, kind: "agent" }),
        ev(2, "agent_spawned", { step: "build", attempt: 1, branch: "impl/4-{slug}" }),
      ],
      [run("build", 1, "running", "agent")],
      null,
    );
    const spawned = row(html, "impl/4-{slug}");
    expect(spawned).toContain("impl/4-{slug}");
    expect(spawned).not.toContain("<a ");
  });

  it("dresses that link as a button in the status colour of its row", () => {
    // A gate waiting on a person reads in the warning role, and so does the chip that asks.
    expect(anchors(render(GATE, GATE_RUNS, HELD), "/pull/248")[0]).toContain(ROLE_SUBTLE.warning);
  });

  it("beats the held link in time with the node the graph is blinking", () => {
    const html = render(GATE, GATE_RUNS, HELD);
    // The graph's own class, worn by the link so the two cannot drift apart.
    expect(anchors(html, "/pull/248")[0]).toContain("flow-blink");
  });

  it("leaves the earlier attempts of the same step still", () => {
    // Attempt 1 of `spec_gate` is decided and cannot be acted on. A page where every attempt of
    // the current step blinks is telling the reader to do three things instead of one.
    const html = render(GATE, GATE_RUNS, HELD);
    for (const tag of anchors(html, "/pull/240")) expect(tag).not.toContain("flow-blink");
  });

  it("greys every link the reader is not being asked to act on", () => {
    // A rejection's row reads red, but its PR is settled: colouring the link would put a second
    // red call-to-action on a page whose one live one is somewhere else entirely.
    const html = render(GATE, GATE_RUNS, HELD);
    for (const tag of anchors(html, "/pull/240")) {
      expect(tag).toContain(ROLE_SUBTLE.neutral);
      expect(tag).not.toContain(ROLE_SUBTLE.danger);
    }
  });

  it("leaves the same link still, and colourless, once the ticket is no longer held there", () => {
    const html = render(GATE, GATE_RUNS, null);
    expect(anchors(html, "/pull/248")[0]).toContain(ROLE_SUBTLE.neutral);
    expect(anchors(html, "/pull/248")[0]).not.toContain(ROLE_SUBTLE.warning);
    expect(anchors(html, "/pull/248")[0]).not.toContain("flow-blink");
  });

  it("says what has to happen only on the attempt that is still waiting", () => {
    const html = render(GATE, GATE_RUNS, HELD);
    const asks = html.match(/Review the pull request, then approve it or request changes/g) ?? [];
    expect(asks).toHaveLength(1);
    // ... and on the row that is asking, not somewhere else in the block.
    expect(row(html, "Review the pull request, then approve it or request changes")).toContain(
      "/pull/248",
    );
  });

  it("keeps every link in the same column, whatever else its row has to say", () => {
    // A rejection carries no fact of its own to print, and without the flexible spacer its link
    // slid left to sit against the label while every other row's link sat at the right margin.
    const html = render(GATE, GATE_RUNS, HELD);
    const rejected = row(html, "Changes requested");
    expect(rejected).toContain("flex-1");
    expect(rejected.indexOf("flex-1")).toBeLessThan(rejected.indexOf("<a "));
  });

  it("marks, without flashing, the link to what failed when a failure stopped the ticket", () => {
    const blocked = [
      ev(1, "agent_spawned", { step: "plan", attempt: 2, branch: "spec/204-remit-to" }),
      ev(2, "escalated", { step: "plan", attempt: 2, pr_number: 251, reason: "ci is red" }),
    ];
    const html = render(blocked, [run("plan", 2, "failed", "agent")], {
      key: "plan#2",
      condition: "blocked",
    });

    // Still, and in the bold danger role with the error icon: a fault is said, not flashed.
    expect(anchors(html, "/pull/251")[0]).not.toContain("flow-blink");
    expect(anchors(html, "/pull/251")[0]).toContain(ROLE_BOLD.danger);
    expect(html).toMatch(
      /\/pull\/251"[^>]*>(?:<!-- -->)?<svg[^>]*data-glyph="status-error"[^>]*aria-label="Failed"/,
    );
    // The spawn row in the same block is bookkeeping: it keeps its chip and stays still, so the
    // page has exactly one thing moving on it.
    expect(anchors(html, "/tree/spec/204-remit-to")[0]).not.toContain("flow-blink");
  });
});

describe("ActivityPanel on an interrupted attempt", () => {
  // The block a reader opens the page to understand. Everything in it used to be dimmed: the two
  // bookkeeping rows because they are bookkeeping, and the two interrupt rows because `factsFor`
  // had no case for them and the unknown-type default renders every event it does not know as
  // bookkeeping too.
  const ENTRIES = [
    ev(1, "step_started", { step: "plan", attempt: 2, kind: "agent" }),
    ev(2, "agent_spawned", {
      step: "plan",
      attempt: 2,
      agent: "plan-agent",
      runtime: "claude_cli",
    }),
    ev(3, "interrupt_requested", { step: "plan", attempt: 2, requested_via: "dashboard" }),
    ev(4, "agent_interrupted", { step: "plan", attempt: 2, run_id: "204-plan-agent-71bc55" }),
  ];
  const RUNS = [run("plan", 2, "interrupted", "agent")];
  const PARKED: Held = { key: "plan#2", condition: "interrupted" };

  it("stops dimming the two rows that say why the ticket is parked", () => {
    const html = render(ENTRIES, RUNS, PARKED);
    expect(labelInk(html, "Interrupt requested")).toBe("text-default");
    expect(labelInk(html, "Interrupted")).toBe("text-default");
  });

  it("keeps dimming the bookkeeping either side of them", () => {
    const html = render(ENTRIES, RUNS, PARKED);
    expect(labelInk(html, "Step started")).toBe("text-subtle");
    expect(labelInk(html, "Agent started")).toBe("text-subtle");
  });

  it("draws them in the warning role, the one the attempt's own tag already reads in", () => {
    const html = render(ENTRIES, RUNS, PARKED);
    // The attempt's tag…
    expect(html).toMatch(/data-appearance="warning"[^>]*>(?:<[^>]+>)*Interrupted</);
    // …and both rows: marked by shape as well as colour, their facts in the same role.
    for (const label of ["Interrupt requested", "Interrupted"]) {
      const parked = row(html, `>${label}<`);
      expect(parked).toContain('data-glyph="status-warning"');
      expect(parked).toContain(ROLE_TEXT.warning);
    }
  });

  it("says how to release the ticket on the attempt it is parked at", () => {
    expect(render(ENTRIES, RUNS, PARKED)).toContain("factorial:interrupted");
  });

  it("says nothing of the sort once the ticket has moved on", () => {
    // The same events, read as history — the ticket resumed and is running again, so the row is a
    // record that a run was stopped, not an instruction to stop it.
    const html = render(
      [
        ...ENTRIES,
        ev(5, "agent_resumed", {
          step: "plan",
          attempt: 2,
          comment_count: 1,
          steered_by: ["operator"],
        }),
      ],
      RUNS,
      null,
    );
    expect(html).not.toContain("factorial:interrupted");
    expect(row(html, "Resumed")).toContain("1 comment from operator");
  });

  it("asks only for the newest park to be released when one attempt parked twice", () => {
    // Idle-parked, released, then interrupted by a person: `factorial:waiting` is gone from the
    // issue, so the earlier row must not ask for it to be removed.
    const html = render(
      [
        ev(1, "step_started", { step: "plan", attempt: 2, kind: "agent" }),
        ev(2, "agent_parked", { step: "plan", attempt: 2, reason: "idle" }),
        ev(3, "agent_resumed", { step: "plan", attempt: 2, comment_count: 0, steered_by: [] }),
        ev(4, "agent_interrupted", { step: "plan", attempt: 2 }),
      ],
      RUNS,
      PARKED,
    );
    expect(row(html, ">Interrupted<")).toContain("factorial:interrupted");
    expect(html).not.toContain("factorial:waiting");
  });
});

describe("the latest event", () => {
  it("is the region's foot read oldest first, and a later flip of the order does not go again", async () => {
    const scrolled = vi.spyOn(Element.prototype, "scrollTo");
    vi.spyOn(Element.prototype, "scrollHeight", "get").mockReturnValue(900);
    const at = (toLatest: number | null, order: ActivityOrder = "oldest") => (
      <ActivityPanel
        segments={segmentsFrom(
          [ev(1, "step_started", { step: "build", attempt: 1, kind: "agent" })],
          [run("build", 1, "succeeded", "agent")],
        )}
        order={order}
        repo={REPO}
        activeKey={null}
        onActiveKeyChange={() => {}}
        runtimeFor={() => undefined}
        onOpenText={() => {}}
        held={null}
        toLatest={toLatest}
      />
    );
    const view = await mount(at(null));
    expect(scrolled).not.toHaveBeenCalled();

    await view.rerender(at(2));
    await view.rerender(at(2, "newest"));
    expect(scrolled.mock.calls).toEqual([[{ top: 900, behavior: "smooth" }], [{ top: 0 }]]);
    await view.unmount();
    vi.restoreAllMocks();
  });
});
