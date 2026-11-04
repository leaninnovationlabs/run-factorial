// @vitest-environment happy-dom
/**
 * The Terminal tab against a fake gateway, a fake emulator and a stubbed `/terminal` read: the
 * writer seat — typing claims it, Take over asks first, a lost seat stops the keys — what each
 * close says and offers, a ticket for a reader with the API token alone, and what the tab offers
 * when there is nothing to attach to: the Waiting banner, Resume with an answer, and Open session.
 * It also covers the loading phases, the route re-reads and the continued-by panel.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../../api/client";
import { ADMIN_PERMISSIONS, holding, MEMBER_PERMISSIONS } from "../../api/organization.fixture";
import {
  TERMINAL_STARTING_MS,
  TERMINAL_UNSETTLED_MS,
  TERMINAL_WAITING_MS,
  terminalPoll,
  trackUnsettled,
  UNSETTLED_FOR_MS,
} from "../../api/queries";
import type { StepRun, TerminalInfo } from "../../api/types";
import { AuthProvider, useAuth } from "../../auth";
import { formatAbsolute } from "../../format";
import { run } from "../../pages/TicketPage.fixture";
import { byText, click, hover, type Mounted, mount, settle, type } from "../../testDom";
import { CloseCode } from "./protocol";
import { TerminalTab } from "./TerminalTab";
import { FakeSocket, xterm } from "./testFakes";

vi.mock("./XtermView", async () => {
  const fakes = await import("./testFakes");
  fakes.xterm.loads += 1;
  return { default: fakes.FakeXterm };
});

const LIVE = run(7, "build", 1, "running") as StepRun;
const PARKED = run(7, "build", 1, "interrupted") as StepRun;
const FINISHED = run(7, "build", 1, "succeeded") as StepRun;

const KEPT_UNTIL = "2026-10-09T14:00:00Z";

function terminal(over: Partial<TerminalInfo> = {}): TerminalInfo {
  return {
    step_run_id: 7,
    gateway_origin: "",
    enabled: true,
    kind: "session",
    attachable: true,
    why_not: null,
    why_not_code: null,
    read_only: null,
    status: "running",
    idle: null,
    parked: null,
    resumable: false,
    resume_pending: false,
    inspect: { status: "none", reason: null, startable: false, unavailable: null },
    kept_until: null,
    settled: false,
    continued_by: null,
    continued_by_attempt: null,
    ...over,
  };
}

const PARKED_IDLE = terminal({
  kind: null,
  attachable: false,
  why_not: "This session is paused. Press Resume session, or open the session read-only.",
  why_not_code: CloseCode.PARKED,
  status: "interrupted",
  parked: {
    reason: "idle",
    question: "Which database should it use?",
    turn_error: null,
    unmet: null,
    at: "2026-10-02T10:00:00Z",
  },
  resumable: true,
  inspect: { status: "none", reason: null, startable: true, unavailable: null },
  kept_until: KEPT_UNTIL,
  settled: true,
});

const ENDED = terminal({
  kind: null,
  attachable: false,
  why_not: "this run has ended; open the session read-only to look at its workspace",
  status: "succeeded",
  inspect: { status: "none", reason: null, startable: true, unavailable: null },
  kept_until: KEPT_UNTIL,
  settled: true,
});

/** A live run whose session is not up yet: the answer is seconds away. */
const STARTING = terminal({
  kind: null,
  attachable: false,
  why_not: "the session is starting",
});

let view: Mounted | null = null;
let client = new QueryClient();
/** What the reader holds in the run's organization: an admin's, unless a test says otherwise. */
let permissions = ADMIN_PERMISSIONS;

beforeEach(() => {
  permissions = ADMIN_PERMISSIONS;
  vi.stubGlobal("WebSocket", FakeSocket);
  FakeSocket.made = [];
  xterm.written = [];
  xterm.parsing = [];
  xterm.focuses = 0;
});

afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

type Drawer = { latest?: StepRun | null; onSelectRun?: (stepRunId: number) => void };

function drawer(shown: StepRun, { latest = shown, onSelectRun = () => {} }: Drawer = {}) {
  return (
    <QueryClientProvider client={client}>
      <AuthProvider>
        <SignOut />
        <TerminalTab run={shown} ticketId={1} latest={latest} onSelectRun={onSelectRun} />
      </AuthProvider>
    </QueryClientProvider>
  );
}

async function show(
  session: string | null,
  shown: StepRun = LIVE,
  info: TerminalInfo | (() => TerminalInfo) = terminal(),
  opened: Drawer = {},
) {
  vi.spyOn(api, "authMe").mockImplementation(async () => {
    if (session === null) throw new ApiError(401, "no session");
    return { email: session };
  });
  vi.spyOn(api, "getRunTerminal").mockImplementation(async () =>
    typeof info === "function" ? info() : info,
  );
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  holding(client, permissions);
  view = await mount(drawer(shown, opened));
  await settle();
  await settle();
  return view.container;
}

function SignOut() {
  const { signOut } = useAuth();
  return (
    <button type="button" onClick={signOut}>
      Sign out
    </button>
  );
}

const last = () => FakeSocket.made.at(-1) as FakeSocket;

async function attach(fields: Record<string, unknown> = {}) {
  await act(async () => {
    last().open();
    last().hello(fields);
  });
}

async function output(bytes: Uint8Array) {
  await act(async () => last().onmessage?.({ data: bytes.buffer }));
}

/** xterm parses what it was handed so far. */
async function parse() {
  await act(async () => {
    while (xterm.parsing.length > 0) xterm.parsing.shift()?.();
  });
}

function hiddenBy(element: Element, names: string[]): boolean {
  for (let at: Element | null = element; at; at = at.parentElement) {
    if (names.some((name) => at?.classList.contains(name))) return true;
  }
  return false;
}

/** On screen for a sighted reader. */
const shown = (element: Element) => !hiddenBy(element, ["sr-only", "invisible", "hidden"]);

/** In the accessibility tree: `sr-only` keeps it there, `invisible` and `hidden` do not. */
const announced = (element: Element) =>
  !hiddenBy(element, ["invisible", "hidden"]) && element.closest('[aria-hidden="true"]') === null;

/** Holds a minimum height whatever it holds. */
const reservesHeight = (element: Element) => /(^|\s)min-h-\d/.test(element.className);

/** The connection's status line, in the row just above the terminal box. */
function statusLine(page: HTMLElement): HTMLElement {
  const box = page.querySelector("[data-fake-xterm]")?.parentElement;
  return box?.previousElementSibling?.querySelector('p[role="status"]') as HTMLElement;
}

/** What a sighted reader sees of the connection's progress: the status line while it shows, the
 *  countdown to a reconnect, and the overlay over the terminal box. */
function progress(page: HTMLElement): string[] {
  const countdown = statusLine(page)?.parentElement?.querySelector<HTMLElement>("[data-countdown]");
  return [statusLine(page), countdown ?? null, byText(page, "span", "Loading the conversation…")]
    .filter((element): element is HTMLElement => element !== null && shown(element))
    .map((element) => element.textContent ?? "");
}

async function drop(code: number, reason = "") {
  await act(async () => last().drop(code, reason));
  await settle();
}

function deferred<T>() {
  let resolve: (value: T) => void = () => {};
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe("TerminalTab", () => {
  it("draws the pty's bytes and clears the screen for each new attach", async () => {
    await show("operator@example.com");
    const before = xterm.resets;
    await attach();
    expect(xterm.resets).toBe(before + 1);
    await act(async () => last().onmessage?.({ data: new Uint8Array([0x68, 0x69]).buffer }));
    expect(xterm.written).toEqual([new Uint8Array([0x68, 0x69])]);
  });

  it("says Connecting, then Attaching, then Loading the conversation, until the history is drawn", async () => {
    const page = await show("operator@example.com");
    expect(progress(page)).toEqual(["Connecting…"]);
    await act(async () => last().open());
    expect(progress(page)).toEqual(["Attaching…"]);
    await act(async () => last().hello());
    expect(progress(page)).toEqual(["Loading the conversation…"]);
    const history = new Uint8Array(150_000);
    const later = new Uint8Array([0x6f, 0x6b]);
    await output(history);
    await output(later);
    expect(xterm.written).toEqual([history]);
    expect(progress(page)).toEqual(["Loading the conversation…"]);
    await parse();
    expect(xterm.written).toEqual([history, later]);
    expect(progress(page)).toEqual([]);
    expect(statusLine(page).textContent).toBe("Connected.");
  });

  it("does not show loading again when a claim re-attaches without history", async () => {
    const page = await show("operator@example.com");
    await attach();
    expect(progress(page)).toEqual(["Loading the conversation…"]);
    await output(new Uint8Array([0x68, 0x69]));
    await parse();
    expect(progress(page)).toEqual([]);
    await act(async () => last().seat({ holder: "operator@example.com", yours: true }));
    await output(new Uint8Array([0x6f, 0x6b]));
    expect(progress(page)).toEqual([]);
    expect(xterm.written.at(-1)).toEqual(new Uint8Array([0x6f, 0x6b]));
  });

  it("keeps the terminal box the same height after it attaches", async () => {
    const page = await show("operator@example.com");
    const row = statusLine(page).parentElement as HTMLElement;
    const header = row.previousElementSibling as HTMLElement;
    expect(reservesHeight(header)).toBe(true);
    expect(reservesHeight(row)).toBe(true);
    await attach();
    expect(announced(statusLine(page))).toBe(true);
    expect(statusLine(page).textContent).toBe("Loading the conversation…");
    await output(new Uint8Array([0x68, 0x69]));
    await parse();
    expect(statusLine(page).parentElement).toBe(row);
    expect(row.previousElementSibling).toBe(header);
    expect(reservesHeight(header)).toBe(true);
    expect(reservesHeight(row)).toBe(true);
    expect(hiddenBy(row, ["sr-only", "invisible", "hidden"])).toBe(false);
    expect(getComputedStyle(row).display).not.toBe("none");
    expect(shown(statusLine(page))).toBe(false);
    expect(announced(statusLine(page))).toBe(true);
    expect(statusLine(page).textContent).toBe("Connected.");
  });

  it("counts down while it waits to reconnect", async () => {
    const page = await show("operator@example.com");
    await attach();
    vi.useFakeTimers();
    const wait = (ms: number) => act(async () => void (await vi.advanceTimersByTimeAsync(ms)));
    // Each failed handshake doubles the wait: 1 s, 2 s, then 4 s.
    await act(async () => last().drop(CloseCode.UNREACHABLE, "The sandbox is not running."));
    await wait(1000);
    await act(async () => last().drop(CloseCode.UNREACHABLE, "The sandbox is not running."));
    await wait(2000);
    await act(async () => last().drop(CloseCode.UNREACHABLE, "The sandbox is not running."));
    await wait(0);
    expect(progress(page)).toEqual(["The sandbox is not running. Reconnecting in 4 s."]);
    expect(statusLine(page).textContent).not.toMatch(/\d+ s\b/);
    await wait(1000);
    expect(progress(page)).toEqual(["The sandbox is not running. Reconnecting in 3 s."]);
    await wait(1000);
    expect(progress(page)).toEqual(["The sandbox is not running. Reconnecting in 2 s."]);
  });

  it("asks for a new sign-in when the cookie expired mid-connection, and stays closed", async () => {
    const page = await show("operator@example.com");
    await attach();
    await drop(CloseCode.SIGNED_OUT, "Your session expired.");
    expect(byText(page, "button", "Sign in again")).not.toBeNull();
    expect(page.textContent).toContain("Your session expired.");
    await settle(1100);
    expect(FakeSocket.made).toHaveLength(1);
  });

  it("asks for a new sign-in when a handshake was refused for want of a session", async () => {
    const page = await show("operator@example.com");
    vi.mocked(api.authMe).mockRejectedValue(new ApiError(401, "no session"));
    await drop(CloseCode.ABNORMAL);
    await settle();
    expect(byText(page, "button", "Sign in again")).not.toBeNull();
    await settle(1100);
    expect(FakeSocket.made).toHaveLength(1);
  });

  it("reconnects after a back-off while the substrate is unreachable", async () => {
    const page = await show("operator@example.com");
    await attach();
    await drop(CloseCode.UNREACHABLE, "The sandbox is not running.");
    expect(page.textContent).toContain("The sandbox is not running. Reconnecting in 1 s.");
    expect(FakeSocket.made).toHaveLength(1);
    await settle(1100);
    expect(FakeSocket.made).toHaveLength(2);
  });

  it("re-reads the route on every reconnect, and shows the ended panel once it stops saying attachable", async () => {
    let answer = terminal();
    const page = await show("operator@example.com", FINISHED, () => answer);
    await attach();
    const reads = vi.mocked(api.getRunTerminal).mock.calls.length;
    answer = ENDED;
    await drop(CloseCode.UNREACHABLE, "The sandbox is not running.");
    await settle();
    expect(api.getRunTerminal).toHaveBeenCalledTimes(reads + 1);
    expect(page.textContent).toContain("This run has ended");
    expect(page.textContent).not.toContain("Reconnecting");
    await settle(1100);
    expect(FakeSocket.made).toHaveLength(1);
  });

  it("re-reads the route when a later attempt of the step appears", async () => {
    await show("operator@example.com", FINISHED, ENDED);
    const reads = vi.mocked(api.getRunTerminal).mock.calls.length;
    const next = run(8, "build", 2, "queued") as StepRun;
    await view?.rerender(drawer(FINISHED, { latest: next }));
    await settle();
    expect(api.getRunTerminal).toHaveBeenCalledTimes(reads + 1);
  });

  it("says a later attempt continues here and links to it, without moving", async () => {
    const onSelectRun = vi.fn();
    const page = await show(
      "operator@example.com",
      FINISHED,
      { ...ENDED, continued_by: 42, continued_by_attempt: 2 },
      { onSelectRun },
    );
    expect(page.textContent).toContain("A later attempt continues in this workspace.");
    expect(onSelectRun).not.toHaveBeenCalled();
    await click(byText(page, "button", "Go to attempt 2"));
    expect(onSelectRun).toHaveBeenCalledExactlyOnceWith(42);
  });

  it("leaves the way to the latest attempt to the drawer's header", async () => {
    const page = await show(
      "operator@example.com",
      FINISHED,
      { ...ENDED, continued_by: 42, continued_by_attempt: 2 },
      { latest: run(42, "build", 2, "running") as StepRun },
    );
    expect(page.textContent).toContain("A later attempt continues in this workspace.");
    expect(byText(page, "button", "Go to attempt 2")).toBeNull();
  });

  it("stops when the session ended", async () => {
    const page = await show("operator@example.com");
    await attach();
    await act(async () => last().text({ type: "exit", reason: "The agent's session ended." }));
    await drop(CloseCode.ENDED);
    expect(page.textContent).toContain("The session ended");
    expect(page.textContent).toContain("The agent's session ended.");
    await settle(1100);
    expect(FakeSocket.made).toHaveLength(1);
  });

  it("dials the gateway where the API says it answers", async () => {
    // One image serves `make up`, where the gateway is another port, and ECS, where it shares
    // this origin, so the bundle holds no address of its own.
    await show("operator@example.com", LIVE, terminal({ gateway_origin: "http://localhost:8001" }));
    expect(last().url).toMatch(/^ws:\/\/localhost:8001\/factorial\/terminal\/runs\/7\b/);
  });

  it("connects by the session cookie, never by a ticket", async () => {
    // Nobody signs in to the dashboard with the API token (design.md §7.19), so every reader
    // holds the cookie the gateway checks against the run's organization.
    await show("operator@example.com");
    expect(last().protocols).toBeUndefined();
    expect(last().url).not.toContain("ticket");
  });

  it("closes its socket when the reader signs out", async () => {
    await show("operator@example.com");
    await attach();
    const socket = last();
    vi.spyOn(api, "authLogout").mockResolvedValue(undefined);
    const assign = vi.fn();
    vi.stubGlobal("location", { assign });
    await click(byText(view?.container ?? document, "button", "Sign out"));
    expect(socket.closedWith).toBe(1000);
    expect(FakeSocket.made).toHaveLength(1);
  });
});

describe("the Waiting banner", () => {
  it("shows the agent's question and when an idle session parks, over its live terminal", async () => {
    const parksAt = new Date(Date.now() + 12.5 * 60_000).toISOString();
    const idle = run(7, "build", 1, "idle") as StepRun;
    const page = await show(
      "operator@example.com",
      idle,
      terminal({
        status: "idle",
        idle: {
          reason: "asked",
          since: null,
          question: "Which database should it use?",
          parks_at: parksAt,
          at_deadline: "park",
        },
      }),
    );
    expect(page.textContent).toContain("Waiting for you");
    expect(page.textContent).toContain("The agent asked:");
    expect(page.textContent).toContain("Which database should it use?");
    expect(page.textContent).toContain("Type below to answer.");
    expect(page.textContent).toContain(
      "If nobody answers within 13 min, the session pauses. You can resume it later.",
    );
    // Still a live session: the banner sits over the terminal, not in its place.
    expect(FakeSocket.made).toHaveLength(1);

    await attach({ seat: { holder: "teammate@example.com", yours: false } });
    expect(page.textContent).toContain("teammate@example.com is answering.");
    expect(page.textContent).not.toContain("Type below to answer.");
  });

  it.each([
    ["past", -5, "If nobody types, the run ends once the step's CI settles"],
    ["ahead", 12.5, "If nobody types, the run ends in 13 min and its step retries."],
  ])("says when a silent stop ends with its deadline %s", async (_, minutes, says) => {
    const idle = run(7, "build", 1, "idle") as StepRun;
    const page = await show(
      "operator@example.com",
      idle,
      terminal({
        status: "idle",
        idle: {
          reason: "turn_end",
          since: null,
          question: null,
          parks_at: new Date(Date.now() + minutes * 60_000).toISOString(),
          at_deadline: "retry",
        },
      }),
    );
    // Past the deadline the run is held by CI the drawer cannot see, not about to end.
    expect(page.textContent).toContain(says);
    expect(page.textContent).not.toContain("any moment now");
  });

  it("offers Resume with an answer once the session parked, and never connects", async () => {
    const page = await show("operator@example.com", PARKED, PARKED_IDLE);
    expect(FakeSocket.made).toEqual([]);
    expect(page.textContent).toContain("Waiting for you");
    expect(page.textContent).toContain("Which database should it use?");
    expect(page.textContent).toContain("factorial:waiting");
    expect(page.querySelector("textarea")).not.toBeNull();
    expect(byText(page, "button", "Resume session")).not.toBeNull();
    expect(page.textContent).toContain(
      "Optional. Leave it empty to reconnect and look first, or type an answer for the agent.",
    );
  });

  it("says a session ended from its terminal, and offers Resume", async () => {
    const ended = terminal({
      ...PARKED_IDLE,
      parked: {
        reason: "exited",
        question: null,
        turn_error: null,
        unmet: null,
        at: "2026-10-02T10:00:00Z",
      },
    });
    const page = await show("operator@example.com", PARKED, ended);
    expect(page.textContent).toContain("Ended from the terminal");
    expect(page.textContent).toContain(
      "Someone ended this session from its terminal before it was accepted. Press Resume session to continue it, or remove the factorial:interrupted label from the issue.",
    );
    expect(byText(page, "button", "Resume session")).not.toBeNull();
  });

  it("says why a session paused on a failed reply and what is still missing, as rules on an older park", async () => {
    const failed = terminal({
      ...PARKED_IDLE,
      parked: {
        reason: "idle",
        question: null,
        turn_error: "401 <b>Invalid</b> bearer token",
        unmet: ['branch_exists("spec/12-*") = false', "pr_open"],
        at: "2026-10-02T10:00:00Z",
      },
    });
    const page = await show("operator@example.com", PARKED, failed);
    expect(page.textContent).toContain("The agent's last reply failed:");
    expect(page.textContent).toContain("401 <b>Invalid</b> bearer token");
    expect(page.textContent).toContain(
      'Still missing: branch_exists("spec/12-*") = false; pr_open.',
    );
    expect(page.querySelector("b")).toBeNull();
    expect(byText(page, "button", "Resume session")).not.toBeNull();
  });

  it("says what is still missing in words when the park recorded them", async () => {
    const worded = terminal({
      ...PARKED_IDLE,
      parked: {
        reason: "idle",
        question: null,
        turn_error: null,
        unmet: ['branch_exists("spec/12-*") = false', 'pr_open("spec/12-*", "main") = false'],
        needs: ["the branch spec/12-…", "an open pull request from spec/12-… into main"],
        at: "2026-10-02T10:00:00Z",
      },
    });
    const page = await show("operator@example.com", PARKED, worded);
    expect(page.textContent).toContain(
      "Still missing: the branch spec/12-…, an open pull request from spec/12-… into main.",
    );
    expect(page.textContent).not.toContain("branch_exists");
  });

  it.each([
    [
      "ceiling",
      "Session time limit reached",
      "The session reached its time limit and paused. Press Resume session to continue. Removing the factorial:interrupted label won't resume it.",
    ],
    [
      "rollover",
      "Session paused",
      "The session paused and couldn't continue on its own. Press Resume session to continue, or remove the factorial:interrupted label from the issue.",
    ],
  ])("words a %s pause and the way to continue it", async (reason, title, text) => {
    const paused = terminal({
      ...PARKED_IDLE,
      parked: { reason, question: null, turn_error: null, unmet: null, at: "2026-10-02T10:00:00Z" },
    });
    const page = await show("operator@example.com", PARKED, paused);
    expect(page.textContent).toContain(`${title}${text}`);
  });

  it("says an earlier attempt cannot be resumed, without offering Resume", async () => {
    const earlier = terminal({ ...PARKED_IDLE, resumable: false });
    const page = await show("operator@example.com", PARKED, earlier);
    expect(page.textContent).toContain(
      "This is an earlier attempt. Only the ticket's current attempt can be resumed.",
    );
    expect(byText(page, "button", "Resume session")).toBeNull();
  });

  it("shows the question and not the failed turn when a parked session has both", async () => {
    const both = terminal({
      ...PARKED_IDLE,
      parked: {
        reason: "idle",
        question: "Which database should it use?",
        turn_error: "401 Invalid bearer token",
        unmet: null,
        at: "2026-10-02T10:00:00Z",
      },
    });
    const page = await show("operator@example.com", PARKED, both);
    expect(page.textContent).toContain("Which database should it use?");
    expect(page.textContent).not.toContain("last reply failed");
    expect(page.textContent).not.toContain("401 Invalid bearer token");
  });

  it("sends the answer with Resume, holds the button while it is pending, then reconnects", async () => {
    let now = PARKED_IDLE;
    const page = await show("operator@example.com", PARKED, () => now);
    const answer = deferred<{ status: string; landed: boolean }>();
    const resume = vi.spyOn(api, "resumeSession").mockReturnValue(answer.promise);

    await type(page.querySelector("textarea"), "Use Postgres.");
    await click(byText(page, "button", "Resume session"));
    await settle();
    expect(resume).toHaveBeenCalledWith(7, "Use Postgres.");
    const button = byText(page, "button", "Resume session");
    expect(button?.getAttribute("aria-busy")).toBe("true");
    await click(button);
    expect(resume).toHaveBeenCalledTimes(1);

    // The loop has not picked it up yet: the button stays held, and says why.
    now = { ...PARKED_IDLE, resume_pending: true };
    await act(async () => answer.resolve({ status: "requested", landed: false }));
    await settle();
    expect(page.textContent).toContain("Resuming. The terminal reconnects in a moment.");
    expect(byText(page, "button", "Resume session")?.getAttribute("aria-disabled")).toBe("true");
    await click(byText(page, "button", "Resume session"));
    expect(resume).toHaveBeenCalledTimes(1);

    // The pass resumed it: the live session is attachable again, and the tab connects.
    now = terminal();
    await settle(2100);
    expect(FakeSocket.made).toHaveLength(1);
    expect(page.querySelector("textarea")).toBeNull();
  });

  it("keeps the answer and says it was not sent when a Resume was already waiting", async () => {
    // Another reader's Resume landed after this tab last read the route: the API records
    // nothing of this one, the answer included.
    let now = PARKED_IDLE;
    const page = await show("operator@example.com", PARKED, () => now);
    now = { ...PARKED_IDLE, resume_pending: true };
    vi.spyOn(api, "resumeSession").mockResolvedValue({ status: "pending", landed: true });
    await type(page.querySelector("textarea"), "Use Postgres.");
    await click(byText(page, "button", "Resume session"));
    await settle();
    expect(page.textContent).toContain("A Resume was already requested. Your answer wasn't sent.");
    expect(page.textContent).not.toContain("Resuming.");
    expect(page.querySelector("textarea")?.value).toBe("Use Postgres.");
  });

  it("resumes with no answer when the box is left blank", async () => {
    const page = await show("operator@example.com", PARKED, PARKED_IDLE);
    const resume = vi
      .spyOn(api, "resumeSession")
      .mockResolvedValue({ status: "requested", landed: true });
    await type(page.querySelector("textarea"), "   ");
    await click(byText(page, "button", "Resume session"));
    expect(resume).toHaveBeenCalledWith(7, null);
  });

  it("turns a parked close from the gateway into the parked banner", async () => {
    let now = terminal();
    const page = await show("operator@example.com", LIVE, () => now);
    await attach();
    now = PARKED_IDLE;
    await drop(
      CloseCode.PARKED,
      "This session is paused. Press Resume session, or open the session read-only.",
    );
    await settle();
    expect(byText(page, "button", "Resume session")).not.toBeNull();
    expect(page.querySelector("textarea")).not.toBeNull();
    expect(page.textContent).toContain("Which database should it use?");
  });
});

describe("without tickets:terminal", () => {
  it("never connects to a live session, and says why", async () => {
    permissions = MEMBER_PERMISSIONS;
    const page = await show(
      "operator@example.com",
      LIVE,
      terminal({ kind: "session", attachable: true }),
    );
    expect(FakeSocket.made).toEqual([]);
    expect(page.textContent).toContain("needs the tickets:terminal permission");
  });

  it("still offers Resume on a parked session, but not Open session", async () => {
    permissions = MEMBER_PERMISSIONS;
    const page = await show("operator@example.com", PARKED, PARKED_IDLE);
    expect(FakeSocket.made).toEqual([]);
    expect(byText(page, "button", "Resume session")).not.toBeNull();
    expect(byText(page, "button", "Open session")).toBeNull();
  });

  it("does not offer Open session on a kept run", async () => {
    permissions = MEMBER_PERMISSIONS;
    const page = await show("operator@example.com", FINISHED, ENDED);
    expect(byText(page, "button", "Open session")).toBeNull();
    expect(page.textContent).toContain("needs the tickets:terminal permission");
  });
});

describe("Open session", () => {
  it("is offered on a kept run, with the date its workspace is kept until", async () => {
    const page = await show("operator@example.com", FINISHED, ENDED);
    expect(FakeSocket.made).toEqual([]);
    expect(byText(page, "button", "Open session")).not.toBeNull();
    expect(page.textContent).toContain(`You can open it until ${formatAbsolute(KEPT_UNTIL)}`);
  });

  it("is not offered once the workspace is gone: the terminal is not available, and why", async () => {
    const page = await show(
      "operator@example.com",
      FINISHED,
      terminal({
        ...ENDED,
        why_not: "this run has ended and its workspace is no longer kept",
        inspect: {
          status: "none",
          reason: null,
          startable: false,
          unavailable: "the workspace was released",
        },
        kept_until: null,
      }),
    );
    expect(byText(page, "button", "Open session")).toBeNull();
    expect(page.textContent).toContain("Terminal not available");
    expect(page.textContent).toContain(
      "This run has no terminal to show. Switch on Transcript to read what it did.",
    );
    expect(page.textContent).toContain("The workspace was released.");
    expect(page.textContent).not.toContain(
      "This run has ended and its workspace is no longer kept.",
    );
    expect(page.textContent).not.toContain("open it until");
  });

  it("asks for a session, polls while it starts, and attaches once it is open", async () => {
    let now = ENDED;
    const page = await show("operator@example.com", FINISHED, () => now);
    const starting = terminal({
      ...ENDED,
      inspect: { status: "starting", reason: null, startable: false, unavailable: null },
    });
    expect(page.textContent).toContain("This run has ended");
    expect(page.textContent).toContain("Open its session read-only to look at its workspace.");
    expect(page.textContent).not.toContain("Terminal not available");
    const open = vi.spyOn(api, "openSession").mockResolvedValue(starting);
    now = starting;
    await click(byText(page, "button", "Open session"));
    await settle();
    expect(open).toHaveBeenCalledWith(7);
    expect(page.textContent).toContain("Opening the session");
    expect(FakeSocket.made).toEqual([]);

    const reads = vi.mocked(api.getRunTerminal).mock.calls.length;
    await settle(2100);
    expect(vi.mocked(api.getRunTerminal).mock.calls.length).toBeGreaterThan(reads);
    expect(FakeSocket.made).toEqual([]);

    now = terminal({
      ...ENDED,
      kind: "inspect",
      attachable: true,
      why_not: null,
      inspect: { status: "open", reason: null, startable: false, unavailable: null },
    });
    await settle(2100);
    expect(FakeSocket.made).toHaveLength(1);
    await attach({ target: "inspect" });
    expect(page.textContent).toContain(
      "Read-only view of the agent's session as it ended. It closes when nobody has it open.",
    );
    // No toggle to take the focus: it goes to the Read-only group, not the body.
    expect(document.activeElement).toBe(byText(page, "span", "Read-only")?.parentElement);
  });

  it("reads the route again after the session view exits, until the loop says it closed", async () => {
    const open = terminal({
      ...ENDED,
      kind: "inspect",
      attachable: true,
      why_not: null,
      inspect: { status: "open", reason: null, startable: false, unavailable: null },
    });
    let now = open;
    const page = await show("operator@example.com", FINISHED, () => now);
    await attach({ target: "inspect" });
    // The viewer quits the session: the gateway closes before the loop's next pass records it.
    await act(async () => last().text({ type: "exit", reason: "The session ended." }));
    await drop(CloseCode.ENDED);
    expect(page.textContent).toContain("The session ended");

    now = terminal({
      ...ENDED,
      inspect: {
        status: "parked",
        reason: "the session ended",
        startable: true,
        unavailable: null,
      },
    });
    await settle(2100);
    expect(page.textContent).toContain("The last open session closed: the session ended.");
  });
});

describe("an open session", () => {
  const OPEN = terminal({
    ...ENDED,
    kind: "inspect",
    attachable: true,
    why_not: null,
    read_only: "an open session is read-only",
    inspect: { status: "open", reason: null, startable: false, unavailable: null },
  });

  it("takes no keystroke and offers no Take over", async () => {
    const page = await show("operator@example.com", FINISHED, OPEN);
    await attach({ target: "inspect", viewers: 2 });
    expect(xterm.props?.typing).toBe("off");
    expect(page.textContent).toContain("Read-only");
    expect(page.textContent).toContain("2 watching");
    expect(page.textContent).toContain("An open session is read-only.");
    expect(byText(page, "button", "Take over")).toBeNull();
    xterm.props?.onData("ls\r", true);
    expect(last().sent).toEqual([]);
  });

  it("closes at once for a lone viewer, with nothing to confirm", async () => {
    const close = vi.spyOn(api, "closeSession").mockResolvedValue(OPEN);
    const page = await show("operator@example.com", FINISHED, OPEN);
    await attach({ target: "inspect", viewers: 1 });
    await click(byText(page, "button", "Close session"));
    await settle();
    expect(close).toHaveBeenCalledWith(7);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });

  it("holds Close session as Closing until the view goes, so a second click sends nothing", async () => {
    const close = vi.spyOn(api, "closeSession").mockResolvedValue(OPEN);
    const page = await show("operator@example.com", FINISHED, OPEN);
    await attach({ target: "inspect", viewers: 1 });
    await click(byText(page, "button", "Close session"));
    await settle();
    const button = byText(page, "button", "Closing…");
    expect(button?.getAttribute("aria-disabled")).toBe("true");
    await click(button);
    expect(close).toHaveBeenCalledTimes(1);
  });

  it("asks before closing a session others are watching", async () => {
    const close = vi.spyOn(api, "closeSession").mockResolvedValue(OPEN);
    const page = await show("operator@example.com", FINISHED, OPEN);
    await attach({ target: "inspect", viewers: 3 });
    await click(byText(page, "button", "Close session"));
    await settle();
    const popup = document.querySelector<HTMLElement>('[role="dialog"]');
    expect(popup?.textContent).toContain("2 others are watching. Closing ends it for everyone.");
    expect(close).not.toHaveBeenCalled();

    await click(byText(popup ?? document, "button", "Close session"));
    expect(close).toHaveBeenCalledWith(7);
  });

  it("holds the confirm trigger as Closing too once a watched session is closed", async () => {
    const close = vi.spyOn(api, "closeSession").mockResolvedValue(OPEN);
    const page = await show("operator@example.com", FINISHED, OPEN);
    await attach({ target: "inspect", viewers: 3 });
    await click(byText(page, "button", "Close session"));
    await settle();
    await click(
      byText(
        document.querySelector<HTMLElement>('[role="dialog"]') ?? document,
        "button",
        "Close session",
      ),
    );
    await settle();
    expect(close).toHaveBeenCalledTimes(1);
    const trigger = byText(page, "button", "Closing…");
    expect(trigger?.getAttribute("aria-disabled")).toBe("true");
    await click(trigger);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(close).toHaveBeenCalledTimes(1);
  });

  it("offers no Close session on a live session", async () => {
    const page = await show("operator@example.com");
    await attach();
    expect(byText(page, "button", "Close session")).toBeNull();
  });
});

describe("who is watching", () => {
  const watching = () => document.querySelector<HTMLElement>("[data-watching]");
  const tooltip = () => document.querySelector('[role="tooltip"]')?.textContent;

  it("shows the count beside an eye, and lists each person once on hover, by name", async () => {
    await show("teammate-2@example.com");
    await attach({
      viewers: 4,
      names: [
        "operator@example.com",
        "teammate-2@example.com",
        "teammate-2@example.com",
        "teammate-3@example.com",
      ],
    });
    expect(watching()?.querySelector('[data-glyph="eye"]')).not.toBeNull();
    // The word is there for a screen reader, beside the number a sighted reader sees.
    expect(watching()?.textContent).toBe("4 watching");

    await hover(watching());
    await settle(350);
    expect(tooltip()).toBe(
      "operator@example.com\nteammate-2@example.com (2 tabs)\nteammate-3@example.com",
    );
  });

  it("keeps the list current as people come and go", async () => {
    await show("teammate-2@example.com");
    await attach({ viewers: 2, names: ["operator@example.com", "teammate-2@example.com"] });
    await act(async () => last().status({ viewers: 1, names: ["teammate-2@example.com"] }));
    await hover(watching());
    await settle(350);
    expect(watching()?.textContent).toBe("1 watching");
    expect(tooltip()).toBe("teammate-2@example.com");
  });

  it("offers no list a gateway did not send", async () => {
    await show("teammate-2@example.com");
    await attach({ viewers: 2 });
    await hover(watching());
    await settle(350);
    expect(watching()?.textContent).toBe("2 watching");
    expect(tooltip()).toBeUndefined();
  });
});

const TEAMMATE = "teammate@example.com";
const held = (holder: string | null = TEAMMATE) => ({
  seat: { holder, yours: false },
  writers: holder === null ? 0 : 1,
  viewers: 2,
});
const sentText = () => last().sent.filter((frame) => typeof frame === "string");

describe("the writer seat", () => {
  it("takes keystrokes while the seat is free: the first claims it, and the keys follow the grant", async () => {
    const page = await show("operator@example.com");
    expect(last().url).toMatch(/\/terminal\/runs\/7\?cols=100&rows=30$/);
    await attach();
    expect(xterm.props?.typing).toBe("free");
    expect(xterm.focuses).toBe(1);
    expect(page.textContent).toContain("Ctrl+] leaves the terminal.");

    await act(async () => xterm.props?.onData("y", true));
    await act(async () => xterm.props?.onData("\r", true));
    expect(sentText()).toEqual(['{"type":"claim"}']);
    expect(last().binary).toEqual([]);

    await act(async () => last().seat({ holder: "operator@example.com", yours: true }));
    expect(last().binary).toEqual([new Uint8Array([0x79, 0x0d])]);
    expect(xterm.props?.typing).toBe("seated");
    await act(async () =>
      last().status({
        ...held("operator@example.com"),
        seat: { holder: "operator@example.com", yours: true },
      }),
    );
    expect(page.textContent).toContain("2 watching · you're typing");
  });

  it("disables stdin and offers Take over while someone else holds the seat", async () => {
    const page = await show("operator@example.com");
    await attach(held());
    expect(xterm.props?.typing).toBe("off");
    expect(page.textContent).toContain(`${TEAMMATE} is typing`);
    expect(page.textContent).toContain(`2 watching · ${TEAMMATE} typing`);
    expect(byText(page, "button", "Take over")).not.toBeNull();
    await act(async () => xterm.props?.onData("x", true));
    expect(last().sent).toEqual([]);
  });

  it("asks before taking the seat, and takes it with force on yes", async () => {
    const page = await show("operator@example.com");
    await attach(held());
    const before = xterm.focuses;
    await click(byText(page, "button", "Take over"));
    await settle();
    const popup = document.querySelector<HTMLElement>('[role="dialog"]');
    expect(popup?.textContent).toContain(`${TEAMMATE} can no longer type here`);
    expect(last().sent).toEqual([]);

    await click(byText(popup ?? document, "button", "Take over"));
    expect(sentText()).toEqual(['{"type":"claim","force":true}']);
    expect(xterm.focuses).toBe(before + 1);
    expect(page.textContent).toContain("Taking over typing…");
    await act(async () => last().seat({ holder: "operator@example.com", yours: true }));
    expect(xterm.props?.typing).toBe("seated");
    expect(byText(page, "button", "Take over")).toBeNull();
  });

  it("disables stdin and says who took over when the seat is lost", async () => {
    const page = await show("operator@example.com");
    await attach();
    await act(async () => xterm.props?.onData("a", true));
    await act(async () => last().seat({ holder: "operator@example.com", yours: true }));
    await act(async () => last().seat({ holder: TEAMMATE, taken: true }));
    expect(xterm.props?.typing).toBe("off");
    expect(page.textContent).toContain(`${TEAMMATE} took over typing`);
    expect(byText(page, "button", "Take over")).not.toBeNull();
    await act(async () => xterm.props?.onData("b", true));
    expect(last().binary).toEqual([new Uint8Array([0x61])]);
  });

  it.each([
    [
      "the route says the ticket moved on",
      { read_only: "the ticket has moved past this step, so this session is read-only" },
      held(null),
    ],
    [
      "the route says the ticket moved on, though someone holds the seat",
      { read_only: "the ticket has moved past this step, so this session is read-only" },
      held(),
    ],
  ])("offers no Take over on a read-only target: %s", async (_, over, hello) => {
    const page = await show("operator@example.com", LIVE, terminal(over));
    await attach(hello);
    expect(xterm.props?.typing).toBe("off");
    expect(page.textContent).toContain(
      "The ticket has moved past this step, so this session is read-only.",
    );
    expect(byText(page, "button", "Take over")).toBeNull();
    expect(page.textContent).not.toContain("Read-only");
    await act(async () => xterm.props?.onData("x", true));
    expect(last().sent).toEqual([]);
  });

  it("re-reads the route when a claim is refused, and its read-only sentence takes over", async () => {
    const moved = "the ticket has moved past this step, so this session is read-only";
    let now = terminal();
    const page = await show("operator@example.com", LIVE, () => now);
    await attach();
    await act(async () => xterm.props?.onData("x", true));
    now = terminal({ read_only: moved });
    const reads = vi.mocked(api.getRunTerminal).mock.calls.length;
    await act(async () => last().seat({ reason: moved }));
    await settle();
    expect(vi.mocked(api.getRunTerminal).mock.calls.length).toBeGreaterThan(reads);
    expect(xterm.props?.typing).toBe("off");
    expect(page.textContent).toContain(
      "The ticket has moved past this step, so this session is read-only.",
    );
    expect(byText(page, "button", "Take over")).toBeNull();
  });

  it("says why a claim was refused for a passing reason, and claims again on the next key", async () => {
    const page = await show("operator@example.com");
    await attach();
    await act(async () => xterm.props?.onData("x", true));
    await act(async () => last().seat({ reason: "the gateway could not check this run" }));
    await settle();
    expect(page.textContent).toContain("The gateway could not check this run.");
    expect(xterm.props?.typing).toBe("free");
    await act(async () => xterm.props?.onData("x", true));
    expect(sentText()).toEqual(['{"type":"claim"}', '{"type":"claim"}']);
  });

  it("never claims a free seat for what the terminal answers on its own", async () => {
    const DA = "\x1b[?1;2c";
    await show("operator@example.com");
    await attach();
    await act(async () => xterm.props?.onData(DA, false));
    expect(last().sent).toEqual([]);

    await act(async () => xterm.props?.onData("y", true));
    await act(async () => last().seat({ holder: "operator@example.com", yours: true }));
    // Freed for want of typing: the gateway re-attaches read-only, and tmux asks again.
    await act(async () =>
      last().seat({ reason: "the keyboard was freed after 120 seconds without typing" }),
    );
    await act(async () => xterm.props?.onData(DA, false));
    expect(sentText()).toEqual(['{"type":"claim"}']);
    expect(last().binary).toEqual([new Uint8Array([0x79])]);
  });

  it("leaves Escape to the page while a claim is out", async () => {
    const page = await show("operator@example.com");
    await attach();
    await act(async () => xterm.props?.onData("y", true));
    expect(page.textContent).toContain("Taking over typing…");
    expect(xterm.props?.typing).toBe("free");
  });

  it("lets the same person move the seat to this tab without asking", async () => {
    const page = await show("operator@example.com");
    await attach(held("operator@example.com"));
    expect(xterm.props?.typing).toBe("off");
    expect(page.textContent).toContain("You're typing in another tab");
    expect(byText(page, "button", "Take over")).toBeNull();
    await click(byText(page, "button", "Type here"));
    expect(sentText()).toEqual(['{"type":"claim","force":true}']);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });

  it("hands the focus to Take over on Ctrl+]", async () => {
    const page = await show("operator@example.com");
    await attach(held());
    await act(async () => xterm.props?.onLeave());
    expect(document.activeElement).toBe(byText(page, "button", "Take over"));
  });
});

describe("terminalPoll", () => {
  it("reads again while the session waits, or a session view starts or a Resume waits (the starting and idle intervals)", () => {
    const idle = {
      reason: "asked",
      since: null,
      question: null,
      parks_at: null,
      at_deadline: "park" as const,
    };
    expect(terminalPoll(terminal({ idle }), null, 0)).toBe(TERMINAL_WAITING_MS);
    expect(terminalPoll(terminal({ ...ENDED, kind: "inspect", attachable: true }), null, 0)).toBe(
      false,
    );
    expect(terminalPoll({ ...PARKED_IDLE, resume_pending: true }, null, 0)).toBe(
      TERMINAL_STARTING_MS,
    );
    expect(
      terminalPoll(
        terminal({
          ...ENDED,
          inspect: { status: "starting", reason: null, startable: false, unavailable: null },
        }),
        null,
        0,
      ),
    ).toBe(TERMINAL_STARTING_MS);
    expect(terminalPoll(terminal(), null, 0)).toBe(false);
    expect(terminalPoll(PARKED_IDLE, null, 0)).toBe(false);
    expect(terminalPoll(ENDED, null, 0)).toBe(false);
  });

  it("reads again every 5 seconds while the answer is unsettled, for at most 10 minutes", () => {
    expect(TERMINAL_UNSETTLED_MS).toBe(5_000);
    expect(UNSETTLED_FOR_MS).toBe(600_000);
    expect(terminalPoll(STARTING, 1_000, 1_000)).toBe(TERMINAL_UNSETTLED_MS);
    expect(terminalPoll(STARTING, 1_000, 1_000 + UNSETTLED_FOR_MS - 1)).toBe(TERMINAL_UNSETTLED_MS);
    expect(terminalPoll(STARTING, 1_000, 1_000 + UNSETTLED_FOR_MS + 1)).toBe(false);
    expect(terminalPoll({ ...STARTING, settled: true }, 1_000, 1_000)).toBe(false);
  });

  it("restarts the unsettled cap when the answer changes", () => {
    let since = trackUnsettled(null, STARTING, 0);
    expect(since?.since).toBe(0);
    since = trackUnsettled(since, STARTING, 5_000);
    expect(since?.since).toBe(0);
    since = trackUnsettled(since, { ...STARTING, settled: true }, 10_000);
    expect(since).toBeNull();
    since = trackUnsettled(since, STARTING, 20_000);
    expect(since?.since).toBe(20_000);
    expect(terminalPoll(STARTING, since?.since ?? null, UNSETTLED_FOR_MS + 1)).toBe(
      TERMINAL_UNSETTLED_MS,
    );
    since = trackUnsettled(since, terminal({ ...STARTING, attachable: true }), 30_000);
    expect(since).toBeNull();
    since = trackUnsettled(since, STARTING, 40_000);
    since = trackUnsettled(since, { ...STARTING, why_not: "the sandbox is resuming" }, 50_000);
    expect(since?.since).toBe(50_000);
  });
});
