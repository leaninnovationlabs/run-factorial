// @vitest-environment happy-dom
/**
 * An `AskUserQuestion` call's question and answer under it. Both are agent- or human-typed
 * strings, so they must reach the page as text, never as markup. Then what a transcript with no
 * turns, or none left, says instead.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { ApiError, api } from "../../api/client";
import type { RunActivityResponse, StepRun } from "../../api/types";
import { run } from "../../pages/TicketPage.fixture";
import { type Mounted, mount, settle } from "../../testDom";
import { TranscriptBody } from "./Transcript";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.restoreAllMocks();
});

const ask = (question: string, answer: string | null, ok: boolean | null = answer !== null) => ({
  tool: "AskUserQuestion",
  target: null,
  ok,
  result: null,
  question,
  answer,
});

it("shows each question and its answer as text, and whether one may still come", async () => {
  const activity: RunActivityResponse = {
    step_run_id: 7,
    runtime_run_id: "7-build-agent-abc",
    turns: [
      {
        index: 1,
        text: [],
        thinking: [],
        tools: [ask("<b>Include</b> archived rows?", "<img src=x onerror=alert(1)>")],
      },
      { index: 2, text: [], thinking: [], tools: [ask("Ship it?", null, null)] },
      { index: 3, text: [], thinking: [], tools: [ask("Merge it?", null, false)] },
    ],
    milestones: [],
    outcome: null,
    cost_usd: null,
    truncated: false,
  };
  vi.spyOn(api, "getRunActivity").mockResolvedValue(activity);
  const shown = run(7, "build", 1, "succeeded", {
    runtime_run_id: "7-build-agent-abc",
  }) as StepRun;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  view = await mount(
    <QueryClientProvider client={qc}>
      <TranscriptBody run={shown} />
    </QueryClientProvider>,
  );
  await settle();

  const log = view.container.querySelector('[role="log"]');
  expect(log?.querySelector("b, img")).toBeNull();
  expect(log?.textContent).toContain("<b>Include</b> archived rows?");
  expect(log?.textContent).toContain("<img src=x onerror=alert(1)>");
  expect(log?.textContent).toMatch(/Ship it\?.*No answer yet/);
  expect(log?.textContent).toMatch(/Merge it\? Answer: No answer(?! yet)/);
});

async function shownFor(status: string, answer: () => Promise<RunActivityResponse>) {
  vi.spyOn(api, "getRunActivity").mockImplementation(answer);
  const shown = run(7, "build", 1, status, { runtime_run_id: "7-build-agent-abc" }) as StepRun;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  view = await mount(
    <QueryClientProvider client={qc}>
      <TranscriptBody run={shown} />
    </QueryClientProvider>,
  );
  await settle();
  return view.container.textContent ?? "";
}

const EMPTY: RunActivityResponse = {
  step_run_id: 7,
  runtime_run_id: "7-build-agent-abc",
  turns: [],
  milestones: [],
  outcome: null,
  cost_usd: null,
  truncated: false,
};

it("says an ended run with no turns recorded none, rather than none yet", async () => {
  const text = await shownFor("succeeded", async () => EMPTY);
  expect(text).toContain("This run recorded no output");
  expect(text).not.toContain("yet");
});

it("says a live run with no turns has captured nothing yet", async () => {
  const text = await shownFor("running", async () => EMPTY);
  expect(text).toContain("Nothing captured yet");
});

it("says a purged transcript was deleted, and what the ticket still holds", async () => {
  const text = await shownFor("succeeded", async () => {
    throw new ApiError(410, "gone");
  });
  expect(text).toContain(
    "Transcript deletedThis run's transcript was deleted after it finished. Its activity and costs are still on the ticket.",
  );
});
