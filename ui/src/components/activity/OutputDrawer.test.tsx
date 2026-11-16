// @vitest-environment happy-dom
/**
 * The Output drawer's sections: which open expanded, that a collapsed one is a bar that expands
 * it again, that the Terminal section switches to the transcript and back, and that the terminal
 * is fetched only for a run that has one and kept through a collapse or a switch.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../../api/client";
import { holding } from "../../api/organization.fixture";
import type { StepRun } from "../../api/types";
import { AuthProvider } from "../../auth";
import { type ForgeHost, forgeHost } from "../../forge";
import { run } from "../../pages/TicketPage.fixture";
import { byText, click, type Mounted, mount, settle } from "../../testDom";
import { FakeSocket, xterm } from "../terminal/testFakes";
import type { ActivitySegment } from "./activity";
import { OutputDrawer } from "./OutputDrawer";

vi.mock("../terminal/XtermView", async () => {
  const fakes = await import("../terminal/testFakes");
  fakes.xterm.loads += 1;
  return { default: fakes.FakeXterm };
});

let view: Mounted | null = null;

beforeEach(() => {
  vi.stubGlobal("WebSocket", FakeSocket);
  FakeSocket.made = [];
  vi.spyOn(api, "authMe").mockResolvedValue({ email: "operator@example.com" });
  // What each section reads stays in flight unless a test answers it: the sections are under test.
  vi.spyOn(api, "getRunActivity").mockReturnValue(new Promise(() => {}));
  vi.spyOn(api, "getRunPreview").mockReturnValue(new Promise(() => {}));
  vi.spyOn(api, "getRunOutputs").mockReturnValue(new Promise(() => {}));
  // The gateway's own answer decides; this read only says whether to ask it.
  vi.spyOn(api, "getRunTerminal").mockImplementation(async (id) => ({
    step_run_id: id,
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
  }));
});

afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const build = (status: string, extra = {}) =>
  run(2, "build", 1, status, { runtime_run_id: "204-build-agent-1", ...extra }) as StepRun;

type Shown = {
  hasPreview?: boolean;
  declaresFiles?: boolean;
  named?: "terminal" | "preview";
  latest?: StepRun | null;
  reviewGate?: string;
  changesOpen?: number;
  segment?: ActivitySegment | null;
  forge?: ForgeHost;
};

const drawerOf = (
  qc: QueryClient,
  shown: StepRun,
  {
    hasPreview = false,
    declaresFiles = false,
    named,
    latest = shown,
    reviewGate,
    changesOpen,
    segment = null,
    forge,
  }: Shown = {},
) => (
  <QueryClientProvider client={qc}>
    <AuthProvider>
      <OutputDrawer
        ticketId={1}
        step="build"
        run={shown}
        segment={segment}
        latest={latest}
        spawn={undefined}
        repo="acme/app"
        forge={forge}
        hasPreview={hasPreview}
        declaresFiles={declaresFiles}
        named={named}
        reviewGate={reviewGate ?? null}
        reviewPr="42"
        changesOpen={changesOpen ?? 0}
        onSelectRun={() => {}}
        onSent={() => {}}
        onClose={() => {}}
      />
    </AuthProvider>
  </QueryClientProvider>
);

const dialog = () => document.querySelector<HTMLElement>('[role="dialog"]') as HTMLElement;

async function open(shown: StepRun, options: Shown = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  holding(qc);
  view = await mount(drawerOf(qc, shown, options));
  await settle();
  return dialog();
}

const sections = (drawer: HTMLElement) =>
  [...drawer.querySelectorAll("section[aria-label]")].map((s) => s.getAttribute("aria-label"));

/** The bars collapsed sections leave: one button each, that expands it. */
const bars = (drawer: HTMLElement) =>
  [...drawer.querySelectorAll<HTMLButtonElement>('button[aria-expanded="false"]')].filter((b) =>
    sections(drawer).includes(b.textContent?.trim() ?? ""),
  );

const bar = (drawer: HTMLElement, name: string) =>
  bars(drawer).find((b) => b.textContent?.trim() === name) ?? null;

const collapse = (drawer: HTMLElement, name: string) =>
  drawer.querySelector<HTMLButtonElement>(`button[aria-label="Collapse ${name}"]`);

const fullWidth = (drawer: HTMLElement, name: string) =>
  drawer.querySelector<HTMLButtonElement>(`button[aria-label="Expand ${name} to full width"]`);

const expanded = (drawer: HTMLElement) =>
  [...drawer.querySelectorAll<HTMLElement>("section[aria-label]")]
    .filter((s) => !s.hidden)
    .map((s) => s.getAttribute("aria-label"));

/** What the drawer shows, left to right: each section, expanded or as its bar. */
const layout = (drawer: HTMLElement) =>
  [...drawer.querySelectorAll<HTMLElement>('button[aria-expanded="false"], section[aria-label]')]
    .filter((e) =>
      e.tagName === "SECTION" ? !e.hidden : bars(drawer).includes(e as HTMLButtonElement),
    )
    .map((e) =>
      e.tagName === "SECTION" ? e.getAttribute("aria-label") : `bar ${e.textContent?.trim()}`,
    );

/** The Terminal section's Transcript switch. */
const transcriptSwitch = (drawer: HTMLElement) =>
  drawer.querySelector<HTMLButtonElement>('section[aria-label="Terminal"] button[role="switch"]');

/** The terminal emulator is on screen, not behind the transcript or a collapse. */
const terminalShown = (drawer: HTMLElement) => {
  const emulator = drawer.querySelector("[data-fake-xterm]");
  return emulator !== null && emulator.closest("[hidden]") === null;
};

describe("OutputDrawer", () => {
  // First in the file: the mocked module counts its first import, which a module cache keeps.
  it("fetches the terminal emulator only for a run that has a terminal", async () => {
    const drawer = await open(build("running", { kind: "action" }));
    expect(drawer.textContent).toContain("Terminal not available");
    expect(xterm.loads).toBe(0);
    expect(FakeSocket.made).toHaveLength(0);
    await view?.unmount();

    const live = await open(build("running"));
    await settle();
    expect(xterm.loads).toBe(1);
    expect(live.querySelector("[data-fake-xterm]")).not.toBeNull();
  });

  it.each(["running", "idle", "interrupted", "succeeded"])(
    "opens a %s run on its Terminal",
    async (status) => {
      const drawer = await open(build(status));
      expect(sections(drawer)).toEqual(["Terminal"]);
      expect(transcriptSwitch(drawer)?.getAttribute("aria-checked")).toBe("false");
    },
  );

  it("opens a step with a preview on its terminal and its preview", async () => {
    const drawer = await open(build("succeeded"), { hasPreview: true });
    expect(expanded(drawer)).toEqual(["Terminal", "Preview"]);
  });

  it("calls the preview Files when the step declares a summary or files", async () => {
    const drawer = await open(build("succeeded"), {
      hasPreview: true,
      declaresFiles: true,
    });
    expect(expanded(drawer)).toEqual(["Terminal", "Files"]);
  });

  it.each([
    ["terminal", ["Terminal", "bar Preview"]],
    ["preview", ["bar Terminal", "Preview"]],
  ] as const)("opens what a link names alone, the other as its bar: %s", async (named, shown) => {
    const drawer = await open(build("succeeded"), {
      hasPreview: true,
      named,
    });
    expect(layout(drawer)).toEqual(shown);
  });

  it("switches to the transcript and back, keeping the terminal connected", async () => {
    const drawer = await open(build("running"));
    await settle();
    const [socket] = FakeSocket.made;
    const shown = drawer.querySelector("[data-fake-xterm]");
    expect(drawer.querySelector('[role="log"][aria-label="Transcript"]')).toBeNull();

    await click(transcriptSwitch(drawer));
    await settle();
    expect(shown?.closest("[hidden]")).not.toBeNull();
    expect(socket?.closedWith).toBeNull();

    await click(transcriptSwitch(drawer));
    expect(shown?.closest("[hidden]")).toBeNull();
    expect(drawer.querySelector("[data-fake-xterm]")).toBe(shown);
    expect(FakeSocket.made).toHaveLength(1);
  });

  it("switches the transcript off when it moves to an attempt that is live", async () => {
    const first = build("succeeded");
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    holding(qc);
    const drawer = await mount(drawerOf(qc, first, { hasPreview: true }));
    view = drawer;
    await settle();
    await click(collapse(dialog(), "Terminal"));
    await click(bar(dialog(), "Terminal"));
    await click(transcriptSwitch(dialog()));
    await click(collapse(dialog(), "Terminal"));
    expect(expanded(dialog())).toEqual(["Preview"]);

    const rework = run(3, "build", 2, "running", {
      runtime_run_id: "204-build-agent-2",
    }) as StepRun;
    await drawer.rerender(drawerOf(qc, rework, { hasPreview: true }));
    await settle();

    expect(expanded(dialog())).toEqual(["Terminal", "Preview"]);
    expect(terminalShown(dialog())).toBe(true);
  });

  it("collapses a section to a bar, which expands it again", async () => {
    const drawer = await open(build("succeeded"), { hasPreview: true });

    await click(collapse(drawer, "Terminal"));
    expect(expanded(drawer)).toEqual(["Preview"]);
    expect(bar(drawer, "Terminal")?.getAttribute("aria-expanded")).toBe("false");

    await click(bar(drawer, "Terminal"));
    expect(expanded(drawer)).toEqual(["Terminal", "Preview"]);
  });

  it("folds a collapsed section into a bar in its own place", async () => {
    const drawer = await open(build("succeeded"), { hasPreview: true });
    await click(collapse(drawer, "Terminal"));
    expect(layout(drawer)).toEqual(["bar Terminal", "Preview"]);

    await click(bar(drawer, "Terminal"));
    await click(collapse(drawer, "Preview"));
    expect(layout(drawer)).toEqual(["Terminal", "bar Preview"]);
  });

  it("draws a collapsed section as a flat bar, with no edge or corner of its own", async () => {
    const drawer = await open(build("succeeded"), { hasPreview: true });
    await click(collapse(drawer, "Terminal"));
    const classes = bar(drawer, "Terminal")?.className.split(/\s+/) ?? [];
    expect(classes).not.toContain("border");
    expect(classes.filter((name) => name.startsWith("rounded"))).toEqual([]);
  });

  it("shows one section full width, collapsing the other", async () => {
    const drawer = await open(build("succeeded"), { hasPreview: true });

    await click(fullWidth(drawer, "Preview"));
    expect(layout(drawer)).toEqual(["bar Terminal", "Preview"]);
    // Already the only one open: there is nothing left to collapse.
    expect(fullWidth(drawer, "Preview")).toBeNull();
  });

  it("keeps the last expanded section open", async () => {
    const drawer = await open(build("succeeded"), { hasPreview: true });
    await click(collapse(drawer, "Preview"));
    expect(collapse(drawer, "Terminal")).toBeNull();
  });

  it("shows one section once the window narrows to a phone", async () => {
    let phone = false;
    const changes = new Set<() => void>();
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        get matches() {
          return phone && query === "(max-width: 767.98px)";
        },
        addEventListener: (_: string, change: () => void) => changes.add(change),
        removeEventListener: (_: string, change: () => void) => changes.delete(change),
      })),
    );
    const drawer = await open(build("succeeded"), { hasPreview: true });
    expect(expanded(drawer)).toEqual(["Terminal", "Preview"]);

    phone = true;
    await act(async () => {
      for (const change of changes) change();
    });
    expect(layout(drawer)).toEqual(["Terminal", "bar Preview"]);
  });

  it("moves the focus to what replaced the control pressed", async () => {
    const drawer = await open(build("succeeded"), { hasPreview: true });

    await click(collapse(drawer, "Terminal"));
    expect(document.activeElement).toBe(bar(drawer, "Terminal"));

    await click(bar(drawer, "Terminal"));
    expect(document.activeElement).toBe(collapse(drawer, "Terminal"));

    await click(fullWidth(drawer, "Preview"));
    expect(document.activeElement).toBe(drawer.querySelector('section[aria-label="Preview"]'));
  });

  it("keeps the terminal connected while its section is collapsed", async () => {
    const drawer = await open(build("running"), { hasPreview: true });
    await settle();
    const [socket] = FakeSocket.made;
    const shown = drawer.querySelector("[data-fake-xterm]");

    await click(collapse(drawer, "Terminal"));
    await settle();
    expect(socket?.closedWith).toBeNull();
    expect(FakeSocket.made).toHaveLength(1);

    await click(bar(drawer, "Terminal"));
    expect(drawer.querySelector("[data-fake-xterm]")).toBe(shown);
  });

  it("offers Copy beside the transcript it copies, only while it shows", async () => {
    vi.mocked(api.getRunActivity).mockResolvedValue({
      step_run_id: 2,
      runtime_run_id: "204-build-agent-1",
      turns: [{ index: 1, text: ["Done."], thinking: [], tools: [] }],
      milestones: [],
      outcome: "success",
      cost_usd: null,
      truncated: false,
    });
    const drawer = await open(build("succeeded"));
    await settle();
    const copy = () =>
      drawer.querySelector('section[aria-label="Terminal"] button[aria-label="Copy transcript"]');
    expect(copy()).toBeNull();

    await click(transcriptSwitch(drawer));
    await settle();
    expect(copy()).not.toBeNull();
    expect(drawer.textContent).toContain("Done.");
  });

  it("links a run's events on the project's own host", async () => {
    const gate = run(5, "review", 1, "succeeded", { kind: "human_gate" }) as StepRun;
    const drawer = await open(gate, {
      segment: {
        key: "review#1",
        nodeId: "review",
        step: "review",
        attempt: 1,
        kind: "human_gate",
        run: gate,
        entries: [
          {
            id: 7,
            type: "gate_waiting",
            details: { step: "review", pr_number: 42 },
            created_at: "2026-08-25T09:00:00Z",
          },
        ],
        status: "info",
        generation: 1,
      },
      forge: {
        kind: "gitlab",
        name: "GitLab",
        origin: "https://gitlab.example.com",
        pull_noun: "MR",
        issue_path: "/-/issues/{key}",
        pull_path: "/-/merge_requests/{number}",
        tree_path: "/-/tree/{branch}",
        commit_path: "/-/commit/{sha}",
      },
    });
    await click(transcriptSwitch(drawer) as HTMLButtonElement);
    const link = [...drawer.querySelectorAll("a")].find((a) => a.textContent?.includes("#42"));
    expect(link?.getAttribute("href")).toBe(
      "https://gitlab.example.com/acme/app/-/merge_requests/42",
    );
  });

  it("words a run's transcript facts and keeps its id behind a copy button", async () => {
    vi.mocked(api.getRunActivity).mockResolvedValue({
      step_run_id: 2,
      runtime_run_id: "204-build-agent-1",
      turns: [
        { index: 1, text: ["Reading."], thinking: [], tools: [] },
        { index: 2, text: ["Still reading."], thinking: [], tools: [] },
      ],
      milestones: [],
      outcome: "error_max_turns",
      cost_usd: null,
      truncated: true,
    });
    const drawer = await open(build("failed"));
    await settle();

    expect(drawer.textContent).toContain(
      "2 turns · Stopped: turn limit reached · Showing the first part of a long transcript",
    );
    expect(drawer.textContent).not.toContain("error_max_turns");
    expect(drawer.textContent).not.toContain("204-build-agent-1");
    expect(byText(drawer, "button", "Copy run id")).not.toBeNull();
  });
});

describe("the change request footer", () => {
  it("names the attempt and the step waiting for review", async () => {
    const drawer = await open(build("succeeded"), { reviewGate: "code_review_gate" });

    expect(drawer.textContent).toContain(
      "Attempt 1 of build is waiting for your review. Approve the pull request on GitHub, or request changes here.",
    );
  });

  it("opens its form at once when asked to, naming the step and the pull request", async () => {
    const drawer = await open(build("succeeded"), {
      reviewGate: "code_review_gate",
      changesOpen: 1,
    });

    const form = drawer.querySelector('form[aria-label="Request changes"]');
    expect(form?.textContent).toContain("What should change in attempt 1 of build?");
    expect(form?.textContent).toContain(
      "This rejects code_review_gate and starts attempt 2 of build, which receives this text as its change request. The text is also posted, with your email, to pull request #42.",
    );
  });

  it("names a GitLab project's merge request and host", async () => {
    const forge = forgeHost({ kind: "gitlab" }, {});
    const drawer = await open(build("succeeded"), { reviewGate: "code_review_gate", forge });
    expect(drawer.textContent).toContain(
      "Approve the merge request on GitLab, or request changes here.",
    );
    await view?.unmount();

    const form = await open(build("succeeded"), {
      reviewGate: "code_review_gate",
      changesOpen: 1,
      forge,
    });
    expect(form.textContent).toContain("posted, with your email, to merge request #42.");
  });
});
