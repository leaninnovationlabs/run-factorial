// @vitest-environment happy-dom
// @vitest-environment-options {"settings":{"disableIframePageLoading":true}}
/**
 * The Output drawer's preview against a polled URL: what it shows before one is published, and that
 * a poll only touches the frame when the answer changed — or when the drawer moves to another
 * attempt, which remounts it even at the same URL.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../../api/client";
import { keys } from "../../api/keys";
import type { OutputEntry, RunOutputs, RunPreview, StepRun } from "../../api/types";
import { AuthProvider } from "../../auth";
import { type ForgeHost, forgeHost } from "../../forge";
import { run } from "../../pages/TicketPage.fixture";
import { choose, click, hover, type Mounted, mount, settle } from "../../testDom";
import { OutputDrawer } from "./OutputDrawer";

let view: Mounted | null = null;

beforeEach(() => {
  vi.spyOn(api, "authMe").mockRejectedValue(new ApiError(401, "no session"));
  outputs({});
});

afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.restoreAllMocks();
});

const client = () =>
  new QueryClient({ defaultOptions: { queries: { retry: false, refetchInterval: false } } });

function viewer(qc: QueryClient, shown: StepRun, latest: StepRun = shown, forge?: ForgeHost) {
  return (
    <QueryClientProvider client={qc}>
      <AuthProvider>
        <OutputDrawer
          ticketId={1}
          step="build"
          run={shown}
          segment={null}
          latest={latest}
          spawn={undefined}
          repo="acme/app"
          forge={forge}
          hasPreview
          declaresFiles={false}
          named="preview"
          reviewGate={null}
          reviewPr={null}
          changesOpen={0}
          onSelectRun={() => {}}
          onSent={() => {}}
          onClose={() => {}}
        />
      </AuthProvider>
    </QueryClientProvider>
  );
}

const NONE: RunPreview = {
  step_run_id: 0,
  urls: [],
  status: "none",
  reason: null,
  served_by_runtime: false,
  startable: false,
  unavailable: null,
  head_sha: null,
  uncommitted: false,
  stops_at: null,
  beside_session: false,
  renewing: false,
  restartable: false,
};

function answers(...previews: Partial<RunPreview>[]) {
  const spy = vi.spyOn(api, "getRunPreview");
  for (const preview of previews) {
    spy.mockImplementationOnce(async (stepRunId) => ({
      ...NONE,
      ...preview,
      step_run_id: stepRunId,
    }));
  }
}

/** The same answer to every poll, for a pane that asks more than once. */
function always(preview: Partial<RunPreview>) {
  vi.spyOn(api, "getRunPreview").mockImplementation(async (stepRunId) => ({
    ...NONE,
    ...preview,
    step_run_id: stepRunId,
  }));
}

/** Each run's output entries, by run id; a run not named has none. */
function outputs(byRun: Record<number, OutputEntry[]>) {
  vi.spyOn(api, "getRunOutputs").mockImplementation(async (stepRunId) => ({
    step_run_id: stepRunId,
    entries: byRun[stepRunId] ?? [],
    cut_off: false,
    changed_files: [],
  }));
}

function entry(over: Partial<OutputEntry>): OutputEntry {
  return {
    status: "published",
    path: null,
    commit: "c".repeat(40),
    size: 10,
    too_large: false,
    matches: [],
    reason: null,
    url: null,
    github_url: null,
    kind: "file",
    key: "file",
    label: "File",
    changed: true,
    group: null,
    group_total: null,
    group_url: null,
    ...over,
  };
}

const declared = (name: string, label: string, over: Partial<OutputEntry> = {}) =>
  entry({
    key: `specs/{ticket}-{slug}/${name}`,
    label,
    path: `specs/12-widget/${name}`,
    url: `/pages/7?blob=${name}&expires=1&signature=s`,
    ...over,
  });
const SUMMARY = declared("summary.html", "Summary");
const SPEC = declared("spec.md", "Spec");
const PLAN = declared("plan.md", "Plan");
const TASKS = declared("tasks.md", "Tasks");
const FOLDER = "https://github.com/acme/widgets/tree/c/reports/12";
const matched = (path: string, label: string, total: number, over: Partial<OutputEntry> = {}) =>
  entry({
    key: `reports/{ticket}/*.html#${path}`,
    label,
    path,
    group: "Reports",
    group_total: total,
    group_url: FOLDER,
    url: `/pages/7?blob=${label}&expires=1&signature=s`,
    ...over,
  });

const tabs = () => [...document.querySelectorAll('[role="tab"]')].map((tab) => tab.textContent);
const tab = (name: string) =>
  [...document.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find(
    (candidate) => candidate.textContent === name,
  ) ?? null;

function publishes(...urls: (string | null)[]) {
  answers(...urls.map((url) => ({ urls: url ? [{ service: null, url }] : [] })));
}

async function poll(qc: QueryClient) {
  await act(() => qc.refetchQueries());
  await settle();
}

const WEB = { service: "web", url: "https://3000-x.e2b.test" };
const frame = () => document.querySelector("iframe");
const build = (id: number, attempt: number) => run(id, "build", attempt, "succeeded") as StepRun;

describe("the preview", () => {
  it("says an ended attempt published no preview", async () => {
    publishes(null);
    view = await mount(viewer(client(), build(7, 1)));
    await settle();

    expect(document.body.textContent).toContain("No previewAttempt 1 didn't publish a preview.");
    expect(document.body.textContent).not.toContain("No preview yet");
    expect(frame()).toBeNull();
  });

  it("says a running attempt's preview appears once the agent publishes one", async () => {
    publishes(null);
    view = await mount(viewer(client(), run(7, "build", 1, "running") as StepRun));
    await settle();

    expect(document.body.textContent).toContain(
      "No preview yetThe preview appears here when the agent publishes one.",
    );
    expect(document.body.textContent).not.toContain("didn't publish a preview");
  });

  it("draws a step's declared summary, not an empty live tab, when no URL is published", async () => {
    outputs({ 7: [SUMMARY] });
    answers({});
    view = await mount(viewer(client(), build(7, 1)));
    await settle();

    expect(frame()?.getAttribute("sandbox")).toBe("allow-popups allow-popups-to-escape-sandbox");
    expect(document.body.textContent).not.toContain("didn't publish a preview");
  });

  it("keeps the frame through an unchanged poll and moves it for a new URL", async () => {
    const qc = client();
    publishes(
      "https://preview.example/a",
      "https://preview.example/a",
      "https://preview.example/b",
    );
    view = await mount(viewer(qc, build(7, 1)));
    await settle();
    const first = frame();
    expect(first?.getAttribute("src")).toBe("https://preview.example/a");
    // No popups or modal dialogs: a preview must not draw over the dashboard (design.md §7.9).
    expect(first?.getAttribute("sandbox")).toBe("allow-scripts allow-same-origin allow-forms");

    await poll(qc);
    expect(frame()).toBe(first);

    await poll(qc);
    expect(frame()?.getAttribute("src")).toBe("https://preview.example/b");
  });

  it("remounts the frame for another attempt at the same URL", async () => {
    const qc = client();
    publishes("https://preview.example/a");
    // Already answered, as it is when a reader goes back to an attempt they had open: no
    // loading state comes between the two frames to remount it by accident.
    qc.setQueryData(keys.runPreview(9), {
      ...NONE,
      step_run_id: 9,
      urls: [{ service: null, url: "https://preview.example/a" }],
    });
    view = await mount(viewer(qc, build(7, 1)));
    await settle();
    const first = frame();

    await view.rerender(viewer(qc, build(9, 2)));
    await settle();

    expect(frame()).not.toBeNull();
    expect(frame()).not.toBe(first);
    expect(frame()?.getAttribute("title")).toBe("build preview, attempt 2");
  });

  it("offers Start on a kept run, and sends it", async () => {
    answers({ served_by_runtime: true, startable: true });
    const start = vi.spyOn(api, "startPreview").mockResolvedValue({
      ...NONE,
      step_run_id: 7,
      served_by_runtime: true,
      status: "starting",
    });
    view = await mount(viewer(client(), build(7, 1), build(9, 2)));
    await settle();

    await click(button("Start preview"));
    await settle();

    expect(start).toHaveBeenCalledWith(7);
    expect(document.body.textContent).toContain("Starting the preview");
  });

  it("frames the first service and links the others", async () => {
    answers({
      served_by_runtime: true,
      status: "ready",
      urls: [
        { service: "web", url: "https://3000-sbx.e2b.app" },
        { service: "api", url: "https://8000-sbx.e2b.app" },
      ],
    });
    view = await mount(viewer(client(), build(7, 1)));
    await settle();

    expect(frame()?.getAttribute("src")).toBe("https://3000-sbx.e2b.app");
    expect(link("Open api")?.getAttribute("href")).toBe("https://8000-sbx.e2b.app");
    expect(button("Stop preview")).not.toBeNull();
  });

  it("says why a preview failed, and offers Start again", async () => {
    answers({
      served_by_runtime: true,
      startable: true,
      status: "failed",
      reason: "setup failed:\nnpm ERR! boom",
    });
    view = await mount(viewer(client(), build(7, 1)));
    await settle();

    expect(document.body.textContent).toContain(
      "The preview couldn't startPress Start preview to try again.",
    );
    expect(document.querySelector("details:not([open]) pre")?.textContent).toBe(
      "setup failed:\nnpm ERR! boom",
    );
    expect(button("Start preview")).not.toBeNull();
  });

  it("warns when the sandbox holds changes no commit has", async () => {
    answers({
      served_by_runtime: true,
      status: "ready",
      uncommitted: true,
      head_sha: "0123abcdef",
      urls: [{ service: "web", url: "https://3000-s.e2b.app" }],
    });
    view = await mount(viewer(client(), build(7, 1)));
    await settle();

    expect(document.body.textContent).toContain("uncommitted changes");
    expect(document.body.textContent).toContain("0123abc");
  });

  it("starts the preview by itself on the latest attempt, once", async () => {
    // The answer is a start the loop declined, so the cache moves on with Start still offered.
    const start = vi.spyOn(api, "startPreview").mockResolvedValue({
      ...NONE,
      step_run_id: 7,
      status: "stopped",
      served_by_runtime: true,
      startable: true,
    });
    always({ status: "none", served_by_runtime: true, startable: true });

    view = await mount(viewer(client(), build(7, 1)));
    await settle();

    expect(start).toHaveBeenCalledOnce();
  });

  it("waits for a click on an older attempt", async () => {
    const start = vi.spyOn(api, "startPreview");
    always({ status: "none", served_by_runtime: true, startable: true });

    view = await mount(viewer(client(), build(7, 1), build(9, 2)));
    await settle();

    expect(start).not.toHaveBeenCalled();
  });

  it("does not start a failed preview by itself", async () => {
    const start = vi.spyOn(api, "startPreview");
    always({ status: "failed", served_by_runtime: true, startable: true, reason: "boom" });

    view = await mount(viewer(client(), build(7, 1)));
    await settle();

    expect(start).not.toHaveBeenCalled();
  });

  it("does not start a renewing preview by itself: the loop already asked", async () => {
    const start = vi.spyOn(api, "startPreview");
    always({ status: "stopped", served_by_runtime: true, startable: true, renewing: true });

    view = await mount(viewer(client(), build(7, 1)));
    await settle();

    expect(start).not.toHaveBeenCalled();
  });

  it("warns five minutes before an idle close and keeps it running on request", async () => {
    const soon = new Date(Date.now() + 4 * 60_000).toISOString();
    always({ status: "ready", served_by_runtime: true, stops_at: soon, urls: [WEB] });
    const keep = vi
      .spyOn(api, "keepPreview")
      .mockResolvedValue({ ...NONE, step_run_id: 7, status: "ready", urls: [WEB] });

    view = await mount(viewer(client(), build(7, 1)));
    await settle();
    expect(document.body.textContent).toContain("Stopping at");
    expect(document.body.textContent).toContain(
      "The preview stops then because nobody is using it.",
    );
    await click(button("Keep running"));

    expect(keep).toHaveBeenCalledOnce();
  });

  it("does not warn while the close is more than five minutes away", async () => {
    const later = new Date(Date.now() + 20 * 60_000).toISOString();
    always({ status: "ready", served_by_runtime: true, stops_at: later, urls: [WEB] });

    view = await mount(viewer(client(), build(7, 1)));
    await settle();

    expect(document.body.textContent).not.toContain("Stopping at");
  });

  it("offers Restart while serving, and says when it runs beside a session", async () => {
    always({
      status: "ready",
      served_by_runtime: true,
      restartable: true,
      beside_session: true,
      urls: [WEB],
    });
    const start = vi
      .spyOn(api, "startPreview")
      .mockResolvedValue({ ...NONE, step_run_id: 7, status: "starting" });

    view = await mount(viewer(client(), build(7, 1)));
    await settle();
    expect(document.body.textContent).toContain(
      "Until it finishes, the app can't call outside services.",
    );
    await click(button("Restart"));

    expect(start).toHaveBeenCalledWith(7, true);
  });

  it("shows a renewing preview as coming back, not as stopped", async () => {
    always({ status: "stopped", served_by_runtime: true, renewing: true });

    view = await mount(viewer(client(), build(7, 1)));
    await settle();

    expect(document.body.textContent).toContain("Restarting the preview");
  });

  it("says a preview stopped for idle, and offers Start", async () => {
    always({ status: "stopped", served_by_runtime: true, startable: true, reason: "idle" });

    view = await mount(viewer(client(), build(7, 1), build(9, 2)));
    await settle();

    expect(document.body.textContent).toContain("Stopped after it sat idle.");
    expect(button("Start preview")).not.toBeNull();
  });

  it("leaves a person's Stop alone after it first saw the preview running", async () => {
    const qc = client();
    const start = vi
      .spyOn(api, "startPreview")
      .mockResolvedValue({ ...NONE, step_run_id: 7, status: "starting" });
    answers(
      { status: "ready", served_by_runtime: true, urls: [WEB] },
      { status: "stopped", served_by_runtime: true, startable: true, reason: "stopped by a@x.dev" },
    );

    view = await mount(viewer(qc, build(7, 1)));
    await settle();
    await poll(qc);

    expect(start).not.toHaveBeenCalled();
    await click(button("Start preview"));
    expect(start).toHaveBeenCalledOnce();
  });

  it("leaves an idle close alone after it first saw the preview running", async () => {
    const qc = client();
    const start = vi.spyOn(api, "startPreview");
    answers(
      { status: "ready", served_by_runtime: true, urls: [WEB] },
      { status: "stopped", served_by_runtime: true, startable: true, reason: "idle" },
    );

    view = await mount(viewer(qc, build(7, 1)));
    await settle();
    await poll(qc);

    expect(start).not.toHaveBeenCalled();
    expect(document.body.textContent).toContain("Stopped after it sat idle.");
  });

  it("leaves a failed renewal alone after it first saw the preview renewing", async () => {
    const qc = client();
    const start = vi.spyOn(api, "startPreview");
    answers(
      { status: "stopped", served_by_runtime: true, startable: true, renewing: true },
      {
        status: "stopped",
        served_by_runtime: true,
        startable: true,
        reason: "renewal\nkilled: out of memory",
      },
    );

    view = await mount(viewer(qc, build(7, 1)));
    await settle();
    await poll(qc);

    expect(start).not.toHaveBeenCalled();
  });

  it("shows the idle warning as the clock reaches five minutes before the close", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    try {
      const closes = new Date(Date.now() + 6 * 60_000).toISOString();
      always({ status: "ready", served_by_runtime: true, stops_at: closes, urls: [WEB] });

      view = await mount(viewer(client(), build(7, 1)));
      await settle();
      expect(document.body.textContent).not.toContain("Stopping at");

      await act(async () => {
        await vi.advanceTimersByTimeAsync(75_000);
      });

      expect(document.body.textContent).toContain("Stopping at");
    } finally {
      vi.useRealTimers();
    }
  });

  it("says why a runtime-served preview cannot start", async () => {
    answers({ served_by_runtime: true, unavailable: "previews run in a cloud sandbox" });
    view = await mount(viewer(client(), build(7, 1)));
    await settle();

    expect(document.body.textContent).toContain("previews run in a cloud sandbox");
    expect(button("Start preview")).toBeNull();
  });
});

function button(name: string): HTMLButtonElement | null {
  return (
    [...document.querySelectorAll("button")].find((b) => b.textContent?.trim() === name) ?? null
  );
}

function link(name: string): HTMLAnchorElement | null {
  return [...document.querySelectorAll("a")].find((a) => a.textContent?.trim() === name) ?? null;
}

describe("the preview's entries", () => {
  it("shows a tab for the summary and each declared file, and frames the one chosen", async () => {
    always({});
    outputs({ 7: [SUMMARY, SPEC, PLAN, TASKS] });
    view = await mount(viewer(client(), build(7, 1)));
    await settle();

    expect(tabs()).toEqual(["Summary", "Spec", "Plan", "Tasks"]);
    expect(tab("Summary")?.getAttribute("aria-selected")).toBe("true");

    await click(tab("Spec"));
    await settle();

    expect(tab("Spec")?.getAttribute("aria-selected")).toBe("true");
    expect(frame()?.getAttribute("src")).toContain("blob=spec.md");
    expect(document.body.textContent).toContain("specs/12-widget/spec.md at ccccccc");
  });

  it("keeps a live preview mounted while a file's tab is shown", async () => {
    always({ urls: [{ service: null, url: "https://preview.example/a" }] });
    outputs({ 7: [SPEC] });
    view = await mount(viewer(client(), build(7, 1)));
    await settle();
    expect(tabs()).toEqual(["Preview", "Spec"]);
    const live = document.querySelector('iframe[src="https://preview.example/a"]');
    expect(live).not.toBeNull();

    await click(tab("Spec"));
    await settle();
    await click(tab("Preview"));
    await settle();

    expect(document.querySelector('iframe[src="https://preview.example/a"]')).toBe(live);
  });

  it("keeps the reader's tab on another attempt that has it, and falls back to the first", async () => {
    const qc = client();
    always({});
    outputs({ 7: [SUMMARY, SPEC], 9: [SUMMARY, SPEC], 11: [SUMMARY] });
    view = await mount(viewer(qc, build(7, 1)));
    await settle();
    await click(tab("Spec"));
    await settle();

    await view.rerender(viewer(qc, build(9, 2)));
    await settle();
    expect(tab("Spec")?.getAttribute("aria-selected")).toBe("true");

    await view.rerender(viewer(qc, build(11, 3)));
    await settle();
    expect(document.querySelector('[role="tablist"]')).toBeNull();
    expect(frame()?.getAttribute("src")).toContain("blob=summary.html");
  });

  it("shows a declared file that was not produced as missing, naming the path", async () => {
    always({});
    outputs({
      7: [
        SPEC,
        declared("plan.md", "Plan", {
          status: "missing",
          path: null,
          url: null,
          reason: "the attempt left no specs/12-widget/plan.md on spec/12-widget",
        }),
      ],
    });
    view = await mount(viewer(client(), build(7, 1)));
    await settle();

    await click(tab("Plan"));
    await settle();

    expect(document.body.textContent).toContain("File not found");
    expect(document.body.textContent).toContain(
      "The attempt left no specs/12-widget/plan.md on spec/12-widget.",
    );
  });

  it("says a file is unchanged in this attempt", async () => {
    always({});
    outputs({ 7: [declared("spec.md", "Spec", { changed: false })] });
    view = await mount(viewer(client(), build(7, 1)));
    await settle();

    expect(document.body.textContent).toContain("unchanged in this attempt");
  });

  it("lists an entry of a kind it does not know as a link", async () => {
    always({});
    outputs({
      7: [
        SPEC,
        entry({
          kind: "comment",
          key: "verdict",
          label: "Verdict",
          github_url: "https://github.com/acme/app/issues/12#c1",
        }),
      ],
    });
    view = await mount(viewer(client(), build(7, 1)));
    await settle();

    await click(tab("Verdict"));
    await settle();

    expect(
      document.querySelector('a[href="https://github.com/acme/app/issues/12#c1"]'),
    ).not.toBeNull();
  });

  it("draws no tab list for a step with one entry", async () => {
    always({});
    outputs({ 7: [SUMMARY] });
    view = await mount(viewer(client(), build(7, 1)));
    await settle();

    expect(document.querySelector('[role="tablist"]')).toBeNull();
    expect(frame()?.getAttribute("src")).toContain("blob=summary.html");
  });

  it("shows no live tab for a step with only declared files until a URL is published", async () => {
    const qc = client();
    answers({}, { urls: [{ service: null, url: "https://preview.example/a" }] });
    outputs({ 7: [SPEC] });
    view = await mount(viewer(qc, build(7, 1)));
    await settle();
    expect(document.querySelector('[role="tablist"]')).toBeNull();
    expect(document.body.textContent).not.toContain("didn't publish a preview");

    await poll(qc);

    expect(tabs()).toEqual(["Preview", "Spec"]);
  });

  it("starts a sandbox preview only when its tab is first shown", async () => {
    const qc = client();
    const start = vi.spyOn(api, "startPreview").mockResolvedValue({
      ...NONE,
      step_run_id: 9,
      status: "stopped",
      served_by_runtime: true,
      startable: true,
    });
    always({ status: "none", served_by_runtime: true, startable: true });
    outputs({ 7: [SPEC], 9: [SPEC] });
    view = await mount(viewer(qc, build(7, 1), build(9, 2)));
    await settle();
    await click(tab("Spec"));
    await settle();

    await view.rerender(viewer(qc, build(9, 2)));
    await settle();
    expect(start).not.toHaveBeenCalled();

    await click(tab("Preview"));
    await settle();
    expect(start).toHaveBeenCalledOnce();
  });

  it("does not start a sandbox behind a file tab while the next attempt's outputs load", async () => {
    const qc = client();
    const start = vi.spyOn(api, "startPreview").mockResolvedValue({
      ...NONE,
      step_run_id: 9,
      status: "stopped",
      served_by_runtime: true,
      startable: true,
    });
    always({ status: "none", served_by_runtime: true, startable: true });
    let release: (answer: RunOutputs) => void = () => {};
    vi.spyOn(api, "getRunOutputs").mockImplementation((stepRunId) =>
      stepRunId === 9
        ? new Promise<RunOutputs>((resolve) => {
            release = resolve;
          })
        : Promise.resolve({
            step_run_id: stepRunId,
            entries: [SPEC],
            cut_off: false,
            changed_files: [],
          }),
    );
    view = await mount(viewer(qc, build(7, 1), build(9, 2)));
    await settle();
    await click(tab("Spec"));
    await settle();

    await view.rerender(viewer(qc, build(9, 2)));
    await settle();
    expect(start).not.toHaveBeenCalled();

    await act(async () =>
      release({ step_run_id: 9, entries: [SPEC], cut_off: false, changed_files: [] }),
    );
    await settle();
    expect(start).not.toHaveBeenCalled();
    expect(tab("Spec")?.getAttribute("aria-selected")).toBe("true");
  });

  it("offers to try again when the step's files could not be checked", async () => {
    always({});
    vi.spyOn(api, "getRunOutputs")
      .mockRejectedValueOnce(new ApiError(404, "step run 7 not found"))
      .mockResolvedValue({ step_run_id: 7, entries: [SUMMARY], cut_off: false, changed_files: [] });
    view = await mount(viewer(client(), build(7, 1)));
    await settle();
    expect(document.body.textContent).toContain("Couldn't check for the step's files");

    await click(button("Try again"));
    await settle();

    expect(frame()?.getAttribute("src")).toContain("blob=summary.html");
  });

  it("frames a pattern's file chosen by its generated name", async () => {
    always({});
    outputs({
      7: [
        SPEC,
        matched("reports/12/a.html", "a.html", 2),
        matched("reports/12/b.html", "b.html", 2),
      ],
    });
    view = await mount(viewer(client(), build(7, 1)));
    await settle();
    expect(tabs()).toEqual(["Spec", "a.html", "b.html"]);

    await click(tab("b.html"));
    await settle();

    expect(frame()?.getAttribute("src")).toContain("blob=b.html");
    const panel = document.querySelector('[role="tabpanel"]');
    expect(document.getElementById(panel?.getAttribute("aria-labelledby") ?? "")).toBe(
      tab("b.html"),
    );
  });

  it("names a panel by its tab when the file's path holds a space", async () => {
    always({});
    outputs({
      7: [SPEC, matched("reports/12/lint report.html", "lint report.html", 1)],
    });
    view = await mount(viewer(client(), build(7, 1)));
    await settle();

    await click(tab("lint report.html"));
    await settle();

    const panel = document.querySelector('[role="tabpanel"]');
    const named = panel?.getAttribute("aria-labelledby") ?? "";
    expect(named).not.toMatch(/\s/);
    expect(document.getElementById(named)).toBe(tab("lint report.html"));
    expect(tab("lint report.html")?.getAttribute("aria-controls")).toBe(panel?.id);
  });

  it("says when a pattern matched more files than it shows, and links to the rest", async () => {
    always({});
    outputs({
      7: [matched("reports/12/a.html", "a.html", 60), matched("reports/12/b.html", "b.html", 60)],
    });
    view = await mount(viewer(client(), build(7, 1)));
    await settle();
    await click(tab("a.html"));
    await settle();

    expect(document.body.textContent).toContain("Reports matched 60 files");
    expect(document.body.textContent).toContain("The first 2, by path, show here.");
    const folder = [...document.querySelectorAll("a")].find(
      (link) => link.textContent === "Open the folder on GitHub",
    );
    expect(folder?.getAttribute("href")).toBe(FOLDER);
  });

  it("names a GitLab project's host on the link to the rest of the folder", async () => {
    always({});
    outputs({
      7: [matched("reports/12/a.html", "a.html", 60), matched("reports/12/b.html", "b.html", 60)],
    });
    const gitlab = forgeHost({ kind: "gitlab" }, {});
    view = await mount(viewer(client(), build(7, 1), build(7, 1), gitlab));
    await settle();
    await click(tab("a.html"));
    await settle();

    const folder = [...document.querySelectorAll("a")].find(
      (link) => link.textContent === "Open the folder on GitLab",
    );
    expect(folder?.getAttribute("href")).toBe(FOLDER);
  });

  it("counts a capped pattern's own files, not every file with its label", async () => {
    always({});
    const other = matched("more/12/c.md", "c.md", 1, {
      key: "more/{ticket}/*.md#more/12/c.md",
      group_url: null,
    });
    outputs({
      7: [
        matched("reports/12/a.html", "a.html", 60),
        matched("reports/12/b.html", "b.html", 60),
        other,
      ],
    });
    view = await mount(viewer(client(), build(7, 1)));
    await settle();
    await click(tab("a.html"));
    await settle();
    expect(document.body.textContent).toContain("The first 2, by path");

    await click(tab("c.md"));
    await settle();
    expect(document.body.textContent).not.toContain("matched");
  });

  it("keeps a reader on a pattern's tab when its pending entry becomes its files", async () => {
    const qc = client();
    const start = vi.spyOn(api, "startPreview").mockResolvedValue({
      ...NONE,
      step_run_id: 9,
      status: "stopped",
      served_by_runtime: true,
      startable: true,
    });
    always({ status: "none", served_by_runtime: true, startable: true });
    const pending = entry({
      key: "reports/{ticket}/*.html",
      label: "Reports",
      status: "pending",
      group: "Reports",
    });
    outputs({ 7: [SPEC, pending], 9: [SPEC, pending] });
    view = await mount(viewer(qc, build(7, 1), build(9, 2)));
    await settle();
    await click(tab("Reports"));
    await settle();
    await view.rerender(viewer(qc, build(9, 2)));
    await settle();
    expect(start).not.toHaveBeenCalled();

    outputs({ 9: [SPEC, matched("reports/12/a.html", "a.html", 1)] });
    await act(async () => {
      await qc.invalidateQueries();
    });
    await settle();

    expect(tab("a.html")?.getAttribute("aria-selected")).toBe("true");
    expect(start).not.toHaveBeenCalled();
  });

  it("words the cut-off notice for patterns", async () => {
    always({});
    vi.spyOn(api, "getRunOutputs").mockResolvedValue({
      step_run_id: 7,
      entries: [SPEC],
      cut_off: true,
      changed_files: [],
    });
    view = await mount(viewer(client(), build(7, 1)));
    await settle();

    expect(document.body.textContent).toContain(
      "This attempt changed more than 300 files, so its record may leave some out. A file named exactly still shows. A pattern shows only the files the record holds.",
    );
  });

  it("shows a long name in full on hover only when it is cut short", async () => {
    always({});
    const long = `${"x".repeat(80)}.html`;
    outputs({ 7: [SPEC, matched(`reports/12/${long}`, long, 1)] });
    view = await mount(viewer(client(), build(7, 1)));
    await settle();
    const name = tab(long)?.querySelector("span") as HTMLElement;
    Object.defineProperty(name, "scrollWidth", { configurable: true, value: 400 });
    Object.defineProperty(name, "clientWidth", { configurable: true, value: 240 });

    await hover(name);
    await settle(400);

    const tips = [...document.querySelectorAll('[role="tooltip"]')].map((tip) => tip.textContent);
    expect(tips).toContain(long);
  });
});

describe("the preview's entries in a narrow drawer", () => {
  let observed: (() => void)[] = [];

  class RecordingResizeObserver {
    constructor(callback: ResizeObserverCallback) {
      observed.push(() => callback([], this as unknown as ResizeObserver));
    }
    observe() {}
    unobserve() {}
    disconnect() {}
  }

  beforeEach(() => {
    observed = [];
    vi.stubGlobal("ResizeObserver", RecordingResizeObserver);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  async function widths(row: number, copy: number) {
    const box = document.querySelector('[data-fits="row"]') as HTMLElement;
    const measured = document.querySelector('[data-fits="measure"]') as HTMLElement;
    Object.defineProperty(box, "clientWidth", { configurable: true, value: row });
    Object.defineProperty(measured, "scrollWidth", { configurable: true, value: copy });
    await act(async () => {
      for (const callback of observed) callback();
    });
    await settle();
  }

  const select = () => document.querySelector("select");
  const options = () => [...(select()?.querySelectorAll("option") ?? [])].map((o) => o.textContent);
  const fileFrame = () => document.querySelector('iframe[src*="blob="]');
  const stepper = (name: string) =>
    document.querySelector<HTMLButtonElement>(`button[aria-label="${name}"]`);
  const counter = () => document.querySelector('[data-fits="count"]')?.textContent;

  it("turns the tabs into one dropdown when they do not fit, and back when they do", async () => {
    always({ urls: [{ service: null, url: "https://preview.example/a" }] });
    outputs({ 7: [SPEC, PLAN] });
    view = await mount(viewer(client(), build(7, 1)));
    await settle();

    await widths(200, 600);
    expect(document.querySelector('[role="tablist"]')).toBeNull();
    expect(options()).toEqual(["Preview", "Spec", "Plan"]);

    await choose(select(), SPEC.key);
    await settle();
    expect(fileFrame()?.getAttribute("src")).toContain("blob=spec.md");

    await widths(800, 600);
    expect(tabs()).toEqual(["Preview", "Spec", "Plan"]);
    expect(tab("Spec")?.getAttribute("aria-selected")).toBe("true");
  });

  it("measures the row when the preview answers after the files", async () => {
    let release: () => void = () => {};
    vi.spyOn(api, "getRunPreview").mockImplementation(
      (stepRunId) =>
        new Promise<RunPreview>((resolve) => {
          release = () => resolve({ ...NONE, step_run_id: stepRunId });
        }),
    );
    outputs({ 7: [SPEC, PLAN] });
    view = await mount(viewer(client(), build(7, 1)));
    await settle();
    await act(async () => release());
    await settle();

    await widths(200, 600);

    expect(select()).not.toBeNull();
  });

  it("steps through the files with Previous and Next", async () => {
    always({});
    outputs({ 7: [SUMMARY, SPEC, PLAN] });
    view = await mount(viewer(client(), build(7, 1)));
    await settle();
    await widths(200, 600);
    expect(counter()).toBe("1 of 3");
    // Unavailable but still focusable: a disabled button would drop a keyboard reader's focus.
    expect(stepper("Previous file")?.getAttribute("aria-disabled")).toBe("true");
    expect(stepper("Previous file")?.disabled).toBe(false);

    await click(stepper("Next file"));
    await settle();
    await click(stepper("Next file"));
    await settle();

    expect(select()?.value).toBe(PLAN.key);
    expect(counter()).toBe("3 of 3");
    expect(stepper("Next file")?.getAttribute("aria-disabled")).toBe("true");
    expect(stepper("Next file")?.disabled).toBe(false);
    expect(fileFrame()?.getAttribute("src")).toContain("blob=plan.md");

    await click(stepper("Next file"));
    await settle();
    expect(counter()).toBe("3 of 3");

    await click(stepper("Previous file"));
    await settle();
    expect(counter()).toBe("2 of 3");
  });

  it("chooses a pattern's file from the dropdown", async () => {
    always({});
    outputs({
      7: [
        SPEC,
        matched("reports/12/a.html", "a.html", 2),
        matched("reports/12/b.html", "b.html", 2),
      ],
    });
    view = await mount(viewer(client(), build(7, 1)));
    await settle();
    await widths(200, 600);

    await choose(select(), "reports/{ticket}/*.html#reports/12/b.html");
    await settle();

    expect(fileFrame()?.getAttribute("src")).toContain("blob=b.html");
  });

  it("names the shown panel by its entry in the dropdown", async () => {
    always({});
    outputs({ 7: [SPEC, PLAN] });
    view = await mount(viewer(client(), build(7, 1)));
    await settle();
    await widths(200, 600);

    expect(document.querySelector('[role="tabpanel"]')).toBeNull();
    expect(document.querySelector('[role="region"][aria-label="Spec"]')).not.toBeNull();
  });

  it("groups each pattern's files under its label in the dropdown", async () => {
    always({});
    const other = matched("more/12/c.md", "c.md", 1, { key: "more/{ticket}/*.md#more/12/c.md" });
    outputs({
      7: [
        SPEC,
        matched("reports/12/a.html", "a.html", 2),
        matched("reports/12/b.html", "b.html", 2),
        other,
      ],
    });
    view = await mount(viewer(client(), build(7, 1)));
    await settle();
    await widths(200, 600);

    const groups = [...document.querySelectorAll('optgroup[label="Reports"]')];
    expect(
      groups.map((group) => [...group.querySelectorAll("option")].map((o) => o.textContent)),
    ).toEqual([["a.html", "b.html"], ["c.md"]]);
  });

  it("never counts the measuring copy as tabs", async () => {
    always({});
    outputs({ 7: [SPEC, PLAN] });
    view = await mount(viewer(client(), build(7, 1)));
    await settle();
    await widths(800, 300);

    expect(tabs()).toEqual(["Spec", "Plan"]);
    expect(
      document.querySelector('[data-fits="measure"]')?.closest('[aria-hidden="true"]'),
    ).not.toBeNull();
  });
});
