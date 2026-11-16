// @vitest-environment happy-dom
/**
 * The ticket page, clicked for real. A refused request must say why, beside the control that
 * made it: the API's 409s carry operator-facing reasons, and the page used to drop them.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../api/client";
import { keys } from "../api/queries";
import type { TicketDetail } from "../api/types";
import { AuthProvider } from "../auth";
import { SLOW_AFTER_MS } from "../components/ui";
import { formatClock } from "../format";
import { byText, click, hover, type Mounted, mount, settle, type } from "../testDom";
import { TicketPage } from "./TicketPage";
import { CONFIG, type Entry, run, seeded, TICKET_ID, type TicketOver } from "./TicketPage.fixture";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.restoreAllMocks();
  vi.useRealTimers();
});

async function page(over: TicketOver = {}, issueState?: string, extra: Entry[] = []) {
  const qc = seeded(extra, over, issueState);
  // Nothing refetches under the test: the seeded answers are the page's whole world.
  qc.setDefaultOptions({ queries: { retry: false, staleTime: Number.POSITIVE_INFINITY } });
  vi.spyOn(api, "authMe").mockResolvedValue({ email: "operator@example.com" });
  view = await mount(
    <QueryClientProvider client={qc}>
      <AuthProvider>
        <MemoryRouter initialEntries={[`/tickets/${TICKET_ID}`]}>
          <Routes>
            <Route path="/tickets/:ticketId" element={<TicketPage />} />
          </Routes>
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
  return view.container;
}

/** Open the action's confirmation and press its confirm, which says the full action ("Exit
 *  ticket") where the button shows the short word ("Exit"). The popup floats over the page, so it
 *  is found in the document, not in `root`. */
async function confirm(root: HTMLElement, label: string, confirmLabel: string) {
  await click(byText(root, "button", label));
  const popup = document.querySelector<HTMLElement>('[role="dialog"]');
  if (popup === null) throw new Error(`pressing ${label} opened no confirmation`);
  await click(byText(popup, "button", confirmLabel));
  await settle();
}

function alerts(root: HTMLElement): string[] {
  return [...root.querySelectorAll('[role="alert"]')].map((node) => node.textContent ?? "");
}

describe("a ticket request the API refuses", () => {
  it("says why an exit failed", async () => {
    vi.spyOn(api, "exitTicket").mockRejectedValue(
      new ApiError(409, "ticket 5204 is done; there is nothing to exit"),
    );
    const root = await page();
    await confirm(root, "Exit", "Exit ticket");
    expect(alerts(root)).toContain("ticket 5204 is done; there is nothing to exit");
  });

  it("says why a re-entry failed", async () => {
    vi.spyOn(api, "reenterTicket").mockRejectedValue(
      new ApiError(409, "ticket 5204 has not exited"),
    );
    const root = await page(
      { status: "exited", current_step: "plan", step_runs: [run(1, "plan", 1, "cancelled")] },
      "open",
    );
    await confirm(root, "Re-enter", "Re-enter ticket");
    expect(alerts(root)).toContain("ticket 5204 has not exited");
  });

  it("says why a requeue failed", async () => {
    vi.spyOn(api, "requeueTicket").mockRejectedValue(
      new ApiError(409, "ticket 5204 is not quarantined"),
    );
    const root = await page({ quarantined_at: "2026-08-25T11:00:00Z" });
    await confirm(root, "Requeue", "Requeue ticket");
    expect(alerts(root)).toContain("ticket 5204 is not quarantined");
  });

  it("says why an interrupt failed", async () => {
    vi.spyOn(api, "interruptRun").mockRejectedValue(new ApiError(409, "step run 1 is not running"));
    const root = await page({
      current_step: "plan",
      status: "running",
      step_runs: [run(1, "plan", 1, "running")],
    });
    await confirm(root, "Interrupt", "Interrupt step");
    expect(alerts(root)).toContain("step run 1 is not running");
  });

  it("asks before it interrupts, the way Exit asks before it exits", async () => {
    const interrupt = vi.spyOn(api, "interruptRun").mockResolvedValue({} as never);
    const root = await page({
      current_step: "plan",
      status: "running",
      step_runs: [run(1, "plan", 1, "running")],
    });

    await click(byText(root, "button", "Interrupt"));
    await settle();
    expect(interrupt).not.toHaveBeenCalled();
    const popup = document.querySelector<HTMLElement>('[role="dialog"]');
    expect(popup?.textContent).toContain("The agent stops and its workspace is kept");

    if (popup) await click(byText(popup, "button", "Interrupt step"));
    await settle();
    expect(interrupt).toHaveBeenCalled();
  });

  it("draws a refused refresh as an error, not as a status line", async () => {
    vi.spyOn(api, "refreshTicket").mockRejectedValue(
      new ApiError(409, "ticket 5204's last 2 pass(es) failed"),
    );
    const root = await page();
    await click(byText(root, "button", "Refresh"));
    await settle();
    expect(alerts(root)).toContain("ticket 5204's last 2 pass(es) failed");
    const status = [...root.querySelectorAll('[role="status"]')].map((n) => n.textContent);
    expect(status).not.toContain("ticket 5204's last 2 pass(es) failed");
  });
});

describe("the action row", () => {
  it("draws every control small, each with one icon before its word", async () => {
    const root = await page();
    const row = root.querySelector<HTMLElement>('[role="group"][aria-label="Ticket actions"]');
    const controls = [...(row?.querySelectorAll<HTMLElement>("button, a") ?? [])];
    const words = controls.map((control) => control.textContent?.trim() ?? "");
    expect(words).toEqual(["Refresh", "Description", "Issue", "Exit"]);
    for (const [index, control] of controls.entries()) {
      const word = words[index] ?? "";
      expect(control.getAttribute("data-spacing"), word).toBe("compact");
      expect(control.querySelectorAll("svg"), word).toHaveLength(1);
      expect(control.innerHTML.indexOf("<svg"), word).toBeLessThan(control.innerHTML.indexOf(word));
    }
    expect(controls[3]?.querySelector("svg")?.getAttribute("data-glyph")).toBe(
      "exclamation-circle",
    );
  });
});

describe("the header", () => {
  it("says a refresh's answer at the end of the facts row, not under the buttons", async () => {
    // Under the buttons it widened the actions group, which then no longer fit beside a long
    // title and wrapped onto a row of its own.
    const world = seeded();
    const ticket = world.getQueryData(keys.ticket(TICKET_ID)) as TicketDetail;
    const requestedAt = "2026-08-25T11:04:07Z";
    vi.spyOn(api, "refreshTicket").mockResolvedValue({
      landed: true,
      requested_at: requestedAt,
      ticket,
    });
    // The request holds until the log it invalidates has been read again.
    vi.spyOn(api, "getTimeline").mockResolvedValue(
      world.getQueryData(keys.timeline(TICKET_ID)) as Awaited<ReturnType<typeof api.getTimeline>>,
    );
    const root = await page();
    await click(byText(root, "button", "Refresh"));
    await settle();

    const note = byText(root, '[role="status"]', `updated at ${formatClock(requestedAt)}`);
    expect(note).not.toBeNull();
    expect(note?.closest('[aria-label="Ticket actions"]')).toBeNull();
    // Beside the facts, pushed to the row's far end.
    expect(note?.parentElement?.textContent).toContain("Admitted");
    expect(note?.className).toContain("ml-auto");
  });

  it("says a slow refresh is waiting in the same place, and nothing under the buttons", async () => {
    vi.spyOn(api, "refreshTicket").mockReturnValue(new Promise(() => {}));
    const root = await page();
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    await click(byText(root, "button", "Refresh"));
    // Twice: TanStack tells the page the request is pending on a zero timeout, and only the
    // render that follows starts the clock that makes it slow.
    await act(async () => {
      vi.advanceTimersByTime(0);
    });
    await act(async () => {
      vi.advanceTimersByTime(SLOW_AFTER_MS);
    });

    const note = byText(root, '[role="status"]', "refreshing…");
    expect(note).not.toBeNull();
    expect(note?.parentElement?.textContent).toContain("Admitted");
    const group = root.querySelector('[aria-label="Ticket actions"]');
    expect(group?.querySelectorAll('[role="status"]')).toHaveLength(0);
  });

  it("shows the whole title on hover once it is cut short", async () => {
    const root = await page();
    const heading = root.querySelector("h1") as HTMLElement;
    const title = byText(heading, "span", "Remit-to address is dropped on re-issued invoices");
    if (title === null) throw new Error("the title has no box of its own");
    Object.defineProperty(title, "scrollWidth", { configurable: true, value: 900 });
    Object.defineProperty(title, "clientWidth", { configurable: true, value: 400 });
    await hover(heading);
    await settle(350);
    expect(document.querySelector('[role="tooltip"]')?.textContent).toBe(
      "#204 Remit-to address is dropped on re-issued invoices",
    );
  });
});

describe("a reading surface", () => {
  const triggers: [string, (root: HTMLElement) => Element | null][] = [
    ["the issue's description", (root) => byText(root, "button", "Description")],
    ["a run's terminal", (root) => byText(root, "button", "Terminal")],
    ["a summary in full", (root) => root.querySelector('button[aria-label^="Read "]')],
  ];

  it.each(triggers)("opens %s in the drawer, which dims the page", async (_, trigger) => {
    // What each one reads stays in flight: the drawer, not its contents, is under test.
    vi.spyOn(api, "getTicketIssue").mockReturnValue(new Promise(() => {}));
    vi.spyOn(api, "getRunActivity").mockReturnValue(new Promise(() => {}));
    vi.spyOn(api, "getRunTerminal").mockReturnValue(new Promise(() => {}));
    const root = await page();
    await click(trigger(root));
    await settle(20);
    // The one drawer Show config and Output open in: modal, over a blanket, sliding in.
    const drawer = document.querySelector('[role="dialog"]');
    expect(drawer?.getAttribute("aria-modal")).toBe("true");
    expect(document.querySelector("[data-blanket]")).not.toBeNull();
    expect(root.inert).toBe(true);
    expect(drawer?.className).toContain("translate-x-0");
  });
});

describe("a confirmation popup", () => {
  it("says what the second press spends over the page, and Cancel leaves the page as it was", async () => {
    const exit = vi.spyOn(api, "exitTicket");
    const root = await page();
    const trigger = byText(root, "button", "Exit");
    expect(trigger?.getAttribute("aria-label")).toBe("Exit ticket");
    await click(trigger);
    const popup = document.querySelector('[role="dialog"][aria-label="Exit this ticket?"]');
    expect(popup?.textContent).toContain(
      "Any running work stops and the ticket leaves the process.",
    );
    // Over the page, not in the header's row: nothing under the buttons moves.
    expect(root.contains(popup)).toBe(false);
    expect(trigger?.getAttribute("aria-expanded")).toBe("true");
    // The safe answer holds the focus while the popup is open.
    expect(document.activeElement?.textContent).toBe("Cancel");

    await click(byText(document.body, "button", "Cancel"));
    await settle();
    expect(document.querySelector('[aria-label="Exit this ticket?"]')).toBeNull();
    expect(trigger?.getAttribute("aria-expanded")).toBe("false");
    expect(exit).not.toHaveBeenCalled();
  });

  it("opens from the quarantine message's remedy too, as the header's one confirmation", async () => {
    const requeue = vi.spyOn(api, "requeueTicket");
    const root = await page({ quarantined_at: "2026-08-25T11:00:00Z" });
    const trigger = byText(root, "button", "Requeue");
    const remedy = byText(root, "button", "Requeue ticket");
    expect(remedy).not.toBeNull();
    await click(remedy);
    await settle();
    expect(document.querySelectorAll('[aria-label="Requeue this ticket?"]')).toHaveLength(1);
    expect(trigger?.getAttribute("aria-expanded")).toBe("true");
    expect(document.activeElement?.textContent).toBe("Cancel");
    // Arming is not doing: the request waits for the second press.
    expect(requeue).not.toHaveBeenCalled();
  });
});

describe("a gate whose pull request closed without a merge", () => {
  it("closes the issue and exits from the gate message, after one confirmation", async () => {
    vi.spyOn(api, "getTicketGatePullRequest").mockResolvedValue({
      ticket_id: TICKET_ID,
      number: 57,
      url: "https://github.com/acme/app/pull/57",
      head_ref: "spec/204-remit-to",
      state: "closed",
      merged: false,
    });
    const exit = vi
      .spyOn(api, "exitTicket")
      .mockResolvedValue({ status: "requested", landed: true } as never);
    const root = await page();
    await settle();
    const message = [...root.querySelectorAll<HTMLElement>("section")].find(
      (section) => section.querySelector("h3")?.textContent === "Pull request closed",
    );
    if (message === undefined) throw new Error("no closed pull request message");
    expect(message.textContent).toContain("Pull request #57 was closed without merging");

    await click(byText(message, "button", "Close and exit"));
    await settle();
    const popup = document.querySelector<HTMLElement>(
      '[role="dialog"][aria-label="Close the issue and exit this ticket?"]',
    );
    expect(popup?.textContent).toContain("This closes issue #204");
    // Arming is not doing: the request waits for the second press.
    expect(exit).not.toHaveBeenCalled();

    if (popup) await click(byText(popup, "button", "Close and exit"));
    await settle();
    expect(exit).toHaveBeenCalledWith(TICKET_ID, { close_issue: true });
  });
});

describe("the retry row", () => {
  it("says the retry runs at about its due time, never exactly at it", async () => {
    // The worker acts on its next tick after the time, so "at" would promise a start the loop
    // does not keep. The fixture's backoff has long since expired, so no clock is running.
    const root = await page();
    await click(root.querySelector('[aria-label="When the retry runs"]'));
    const popup = document.body.querySelector('[role="dialog"][aria-label="When the retry runs"]');
    expect(popup?.textContent).toContain("Starts at about");
  });
});

describe("a run's output", () => {
  /** The page at `search`, signed in, with every read the drawer makes left in flight: which run
   *  and which sections the drawer opens on is what is under test. */
  async function at(
    search: string,
    over: TicketOver = {},
    config: Record<string, unknown> = CONFIG,
  ) {
    vi.spyOn(api, "authMe").mockResolvedValue({ email: "operator@example.com" });
    vi.spyOn(api, "getRunActivity").mockReturnValue(new Promise(() => {}));
    vi.spyOn(api, "getRunTerminal").mockReturnValue(new Promise(() => {}));
    vi.spyOn(api, "getRunPreview").mockReturnValue(new Promise(() => {}));
    vi.spyOn(api, "getRunOutputs").mockReturnValue(new Promise(() => {}));
    const qc = seeded([], over, undefined, config);
    qc.setDefaultOptions({ queries: { retry: false, staleTime: Number.POSITIVE_INFINITY } });
    view = await mount(
      <QueryClientProvider client={qc}>
        <AuthProvider>
          <MemoryRouter initialEntries={[`/tickets/${TICKET_ID}${search}`]}>
            <Routes>
              <Route path="/tickets/:ticketId" element={<TicketPage />} />
            </Routes>
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    );
    await settle(20);
    return view.container;
  }

  const drawer = () => document.querySelector<HTMLElement>('[role="dialog"]');
  /** The drawer's sections, and of those the ones expanded: a collapsed one is hidden. */
  const sections = () =>
    [...(drawer()?.querySelectorAll<HTMLElement>("section[aria-label]") ?? [])].map((section) => ({
      name: section.getAttribute("aria-label"),
      expanded: !section.hidden,
    }));
  const expanded = () => sections().flatMap(({ name, expanded }) => (expanded ? [name] : []));

  it("opens the run an ?output= link names on its terminal", async () => {
    await at("?output=2");
    expect(drawer()?.textContent).toContain("plan, attempt 2");
    expect(sections().map(({ name }) => name)).toEqual(["Terminal"]);
  });

  const parked = {
    status: "blocked",
    current_step: "plan",
    park_reason: "idle",
    step_runs: [run(1, "plan", 1, "interrupted", { runtime_run_id: "204-plan-agent-1" })],
  };

  it("expands the terminal of the run a ?terminal= link names", async () => {
    await at("?terminal=1", parked);
    expect(drawer()?.textContent).toContain("plan, attempt 1");
    expect(expanded()).toEqual(["Terminal"]);
  });

  it("ignores a ?terminal= link to a run that is not this ticket's", async () => {
    await at("?terminal=999");
    expect(drawer()).toBeNull();
  });

  it("says the ticket waits on a person, and opens its terminal from there", async () => {
    const root = await at("", parked);
    expect(root.textContent).toContain("Waiting for you");
    await click(byText(root, "button", "Open terminal"));
    await settle(20);
    expect(drawer()?.textContent).toContain("plan");
    expect(expanded()).toEqual(["Terminal"]);
  });

  it("opens a step's files alone from its block's Files button", async () => {
    const files = {
      ...CONFIG,
      steps: CONFIG.steps.map((step) =>
        step.name === "plan" ? { ...step, preview: { files: ["spec.md"] } } : step,
      ),
    };
    const root = await at("", {}, files);
    const header = [...root.querySelectorAll("header")].find((found) =>
      found.textContent?.includes("Attempt 2"),
    ) as HTMLElement;
    await click(byText(header, "button", "Files"));
    await settle(20);
    expect(drawer()?.textContent).toContain("plan, attempt 2");
    expect(expanded()).toEqual(["Files"]);
  });

  it("opens a parked run's terminal, where Resume is, from its block's header", async () => {
    const root = await at("", parked);
    await click(byText(root, "header button", "Terminal"));
    await settle(20);
    expect(sections().map(({ name }) => name)).toEqual(["Terminal"]);
    expect(expanded()).toEqual(["Terminal"]);
  });
});

describe("a retry of a failed step", () => {
  it("starts from the failed attempt's header once the reader confirms in its popup", async () => {
    const retry = vi
      .spyOn(api, "retryStep")
      .mockResolvedValue({ status: "requested", landed: true });
    const due = new Date(Date.now() + 5 * 60_000).toISOString();
    const root = await page(
      {
        current_step: "plan",
        status: "running",
        step_runs: [run(1, "plan", 1, "failed", { runtime_run_id: "204-plan-agent-7f3a2c" })],
      },
      undefined,
      [
        {
          id: 99,
          type: "retry_scheduled",
          details: { step: "plan", attempt: 1, next_attempt: 2, max_attempts: 2, due_at: due },
          created_at: new Date().toISOString(),
        },
      ],
    );

    await click(byText(root, "header button", "Retry"));
    const popup = document.querySelector<HTMLElement>('[role="dialog"][aria-label="Retry plan?"]');
    expect(popup?.textContent).toContain("It starts now instead of at");
    await click(byText(popup as HTMLElement, "button", "Retry step"));
    await settle();

    expect(retry).toHaveBeenCalledWith(1);
  });
});

describe("a restart", () => {
  it("reads the snapshot the ticket now pins, not the one cached for its old pin", async () => {
    const read = vi.spyOn(api, "getTicketProcess").mockResolvedValue({
      ticket_id: TICKET_ID,
      process_version_id: 18,
      process_key: "standard",
      source_commit_sha: "f0e1d2c3b4a5968778695a4b3c2d1e0f9a8b7c6d",
      content: CONFIG,
    });
    const qc = seeded();
    qc.setDefaultOptions({ queries: { retry: false, staleTime: Number.POSITIVE_INFINITY } });
    view = await mount(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[`/tickets/${TICKET_ID}`]}>
          <Routes>
            <Route path="/tickets/:ticketId" element={<TicketPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(read).not.toHaveBeenCalled();

    const ticket = qc.getQueryData(keys.ticket(TICKET_ID)) as TicketDetail;
    await act(async () => {
      qc.setQueryData(keys.ticket(TICKET_ID), { ...ticket, process_version_id: 18, generation: 2 });
    });
    await settle();
    expect(read).toHaveBeenCalledOnce();
  });
});

describe("Restart on latest", () => {
  const PINNED = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0";
  const LATEST = "f0e1d2c3b4a5968778695a4b3c2d1e0f9a8b7c6d";
  const BEHIND: TicketOver = { lane_head: { process_version_id: 18, sha: LATEST } };
  const restartButton = (root: HTMLElement) => byText(root, "button", "Restart on latest");

  const cases: [string, TicketOver, string | undefined, boolean][] = [
    ["a ticket its lane has moved past", BEHIND, undefined, true],
    [
      "an exited ticket its lane has moved past",
      {
        ...BEHIND,
        status: "exited",
        current_step: "plan",
        step_runs: [run(1, "plan", 1, "cancelled")],
      },
      "open",
      true,
    ],
    ["a ticket on its lane's current snapshot", {}, undefined, false],
    [
      "a done ticket",
      { ...BEHIND, status: "done", current_step: "done", step_runs: [] },
      undefined,
      false,
    ],
    [
      "a quarantined ticket",
      { ...BEHIND, quarantined_at: "2026-08-25T11:00:00Z" },
      undefined,
      false,
    ],
    ["a ticket whose lane is retired", { lane_head: null }, undefined, false],
  ];

  it.each(cases)("is offered, or not, on %s", async (_, over, issue, offered) => {
    const root = await page(over, issue);
    expect(restartButton(root) !== null).toBe(offered);
  });

  it("draws it like the rest of the row: small, one icon before its word", async () => {
    const control = restartButton(await page(BEHIND));
    expect(control?.getAttribute("data-spacing")).toBe("compact");
    expect(control?.querySelectorAll("svg")).toHaveLength(1);
  });

  it("asks in a warning modal, then restarts on the snapshot the modal showed", async () => {
    const world = seeded();
    vi.spyOn(api, "getRestartPreview").mockResolvedValue({
      current: { process_version_id: 17, sha: PINNED },
      target: { process_version_id: 18, sha: LATEST },
      current_pin: false,
      live_run: null,
      parked_run: null,
      close_prs: [],
      delete_branches: [],
      merged_outside: [],
      blockers: [],
    });
    const restart = vi
      .spyOn(api, "restartTicket")
      .mockResolvedValue({ status: "requested", landed: true });
    // The request holds until the page it invalidates has been read again.
    vi.spyOn(api, "getTicket").mockResolvedValue(
      world.getQueryData(keys.ticket(TICKET_ID)) as TicketDetail,
    );
    vi.spyOn(api, "getTimeline").mockResolvedValue(
      world.getQueryData(keys.timeline(TICKET_ID)) as Awaited<ReturnType<typeof api.getTimeline>>,
    );
    const root = await page(BEHIND);

    await click(restartButton(root));
    await settle();
    const modal = document.querySelector<HTMLElement>('[role="dialog"]');
    expect(modal?.getAttribute("aria-modal")).toBe("true");
    expect(modal?.querySelector('svg[data-glyph="status-warning"]')).not.toBeNull();
    expect(modal?.querySelector("h1")?.textContent).toBe("Restart #204 on the latest process?");
    expect(root.inert).toBe(true);
    expect(restart).not.toHaveBeenCalled();

    await click(byText(modal as HTMLElement, "button", "Restart ticket"));
    await settle(20);
    expect(restart).toHaveBeenCalledExactlyOnceWith(TICKET_ID, { process_version_id: 18 });
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });
});

describe("Force complete", () => {
  it("keeps the answer on screen after an accept moves the ticket on, then lets go of the run", async () => {
    const failed = run(1, "plan", 1, "failed", { ended_at: "2026-08-25T09:24:18Z" });
    const timeline = seeded().getQueryData(keys.timeline(TICKET_ID));
    const moved = seeded().getQueryData<TicketDetail>(keys.ticket(TICKET_ID));
    vi.spyOn(api, "getTimeline").mockResolvedValue(timeline as never);
    // The pass the request woke accepted the run: the ticket waits at the gate, whose run is
    // the latest now.
    vi.spyOn(api, "getTicket").mockResolvedValue({
      ...moved,
      current_step: "spec_gate",
      status: "waiting_gate",
      step_runs: [
        { ...failed, status: "succeeded" },
        run(3, "spec_gate", 1, "waiting", { kind: "human_gate" }),
      ],
    } as never);
    const ask = vi.spyOn(api, "requestCompletion").mockResolvedValue({
      status: "requested",
      landed: true,
      request_id: 77,
      outcome: "accepted",
      reason: null,
      failures: [],
      overridable: false,
    });
    const root = await page({ current_step: "plan", status: "running", step_runs: [failed] });

    await click(byText(root, "button", "Force complete"));
    await settle();
    await settle();

    expect(ask).toHaveBeenCalledTimes(1);
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]');
    expect(dialog?.textContent).toContain("Its expected results are met. Attempt 1 is accepted");

    if (dialog) await click(byText(dialog, "button", "Close"));
    await settle();
    const buttons = [...root.querySelectorAll("button")].filter(
      (button) => button.textContent === "Force complete",
    );
    // Only the gate's, which a review passes.
    expect(buttons.map((button) => button.getAttribute("aria-disabled"))).toEqual(["true"]);
    // The reason is the button's description, hidden from sight; hover or focus shows it.
    const reason = document.getElementById(buttons[0]?.getAttribute("aria-describedby") ?? "");
    expect(reason?.textContent).toBe("A gate step completes only through its review.");
    expect(reason?.className).toContain("sr-only");
  });
});

describe("a change request sent from the Output drawer (#453)", () => {
  const request: Entry = {
    id: 7,
    type: "step_changes_requested",
    details: {
      step: "spec_gate",
      attempt: 1,
      reviewed_step: "plan",
      reviewed_step_run_id: 2,
      reviewed_attempt: 2,
      feedback: "Keep the remit-to address on a re-issue.",
      requested_by: "operator@example.com",
    },
    created_at: "2026-08-25T10:20:00Z",
  };

  /** The page at the gate, with the Request changes form open in the Output drawer and filled
   *  in. The refetch the request makes answers with the log as `answered` has it. */
  async function filledIn(answered: Entry[]) {
    vi.spyOn(api, "authMe").mockResolvedValue({ email: "operator@example.com" });
    vi.spyOn(api, "getRunActivity").mockReturnValue(new Promise(() => {}));
    vi.spyOn(api, "getRunTerminal").mockReturnValue(new Promise(() => {}));
    vi.spyOn(api, "getTicket").mockResolvedValue(
      seeded().getQueryData(keys.ticket(TICKET_ID)) as never,
    );
    vi.spyOn(api, "getTimeline").mockResolvedValue(
      seeded(answered).getQueryData(keys.timeline(TICKET_ID)) as never,
    );
    vi.spyOn(api, "requestChanges").mockResolvedValue({
      status: "requested",
      landed: true,
      request_id: request.id,
      outcome: "accepted",
      reason: null,
    });
    const qc = seeded();
    qc.setDefaultOptions({ queries: { retry: false, staleTime: Number.POSITIVE_INFINITY } });
    view = await mount(
      <QueryClientProvider client={qc}>
        <AuthProvider>
          <MemoryRouter initialEntries={[`/tickets/${TICKET_ID}`]}>
            <Routes>
              <Route path="/tickets/:ticketId" element={<TicketPage />} />
            </Routes>
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    );
    await settle(20);
    await click(byText(view.container, "button", "Request changes"));
    await settle(20);
    const drawer = document.querySelector<HTMLElement>('[role="dialog"]');
    await type(drawer?.querySelector("textarea"), "Keep the remit-to address on a re-issue.");
    return { qc, drawer };
  }

  const toLatest = { top: 0, behavior: "smooth" };

  it("slides the drawer shut and takes the activity to the latest event", async () => {
    const scrolled = vi.spyOn(Element.prototype, "scrollTo");
    const { drawer } = await filledIn([request]);
    expect(drawer?.textContent).toContain("plan, attempt 2");

    if (drawer) await click(byText(drawer, "button", "Send change request"));
    await settle(20);
    // Leaving the way Close leaves, not gone at once.
    expect(document.querySelector('[role="dialog"]')?.className).toContain("translate-x-full");
    await settle(260);

    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(scrolled).toHaveBeenCalledWith(toLatest);
  });

  it("goes to the latest event only once the log holds the request", async () => {
    const scrolled = vi.spyOn(Element.prototype, "scrollTo");
    const { qc, drawer } = await filledIn([]);

    if (drawer) await click(byText(drawer, "button", "Send change request"));
    await settle(260);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(scrolled).not.toHaveBeenCalledWith(toLatest);

    await act(async () => {
      qc.setQueryData(
        keys.timeline(TICKET_ID),
        seeded([request]).getQueryData(keys.timeline(TICKET_ID)),
      );
    });
    await settle(20);
    expect(scrolled).toHaveBeenCalledWith(toLatest);
  });
});
