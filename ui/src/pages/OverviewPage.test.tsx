// @vitest-environment happy-dom
/**
 * The Overview's applied state, pressed rather than rendered: the address follows every press,
 * and the page follows the address.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Link, MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import { keys } from "../api/queries";
import type { OverviewResponse } from "../api/types";
import { byText, click, type Mounted, mount } from "../testDom";
import { OverviewPage } from "./OverviewPage";

const DATA: OverviewResponse = {
  projects: [
    { id: 1, name: "widgets", active: true },
    { id: 2, name: "toy-paused", active: false },
  ],
  tickets: [
    {
      ticket_id: 1,
      project_id: 1,
      external_key: "acme/repo#12",
      title: "Fix the thing",
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
    },
    {
      ticket_id: 2,
      project_id: 2,
      external_key: "acme/toy#3",
      title: "Toy ticket",
      current_step: "build",
      process_key: "standard",
      latest_run_status: null,
      park_reason: null,
      status: "running",
      runs: 1,
      attempt: 1,
      cost_usd: "0.10",
      cost_estimated: false,
      tokens: 1_000,
      minutes: 2,
      quarantined_at: null,
    },
  ],
  done_days: 7,
};

function Where() {
  const location = useLocation();
  return (
    <>
      <output data-where>{location.search}</output>
      <Link to="/overview">Overview</Link>
    </>
  );
}

let mounted: Mounted | null = null;
afterEach(async () => {
  await mounted?.unmount();
  mounted = null;
});

async function open(url = "/overview") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  client.setQueryData(keys.overview, DATA);
  mounted = await mount(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[url]}>
        <Where />
        <OverviewPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return mounted.container;
}

const where = (root: HTMLElement) =>
  new URLSearchParams(root.querySelector("[data-where]")?.textContent ?? "");
const tile = (root: HTMLElement, tone: string) =>
  root.querySelector<HTMLButtonElement>(`[data-tone="${tone}"]`);

describe("the Overview's address", () => {
  it("pins a metric into the address, and clears it from the tag", async () => {
    const root = await open();
    await click(tile(root, "shade_cost"));
    expect(where(root).get("metric")).toBe("cost");
    expect(tile(root, "shade_cost")?.getAttribute("aria-pressed")).toBe("true");
    expect(byText(root, "span", "Metric: Cost")).not.toBeNull();

    await click(root.querySelector('[aria-label="Clear metric"]'));
    expect(where(root).has("metric")).toBe(false);
    expect(tile(root, "shade_cost")?.getAttribute("aria-pressed")).toBe("false");
  });

  it("writes a lane focus, and Reset view takes everything out again", async () => {
    const root = await open("/overview?metric=time");
    await click(byText(root, "button", "Running1"));
    expect(where(root).get("focus")).toBe("lane:running");

    await click(byText(root, "button", "Reset view"));
    expect([...where(root).keys()]).toEqual([]);
    expect(tile(root, "shade_time")?.getAttribute("aria-pressed")).toBe("false");
    expect(root.textContent).toContain("Nothing is applied to the board.");
  });

  it("follows the address when it changes under the page", async () => {
    // The navigation's own Overview link lands on the bare route with the page still mounted.
    const root = await open("/overview?metric=cost");
    expect(tile(root, "shade_cost")?.getAttribute("aria-pressed")).toBe("true");
    await click(byText(root, "a", "Overview"));
    expect(tile(root, "shade_cost")?.getAttribute("aria-pressed")).toBe("false");
  });
});

describe("the Overview's admission filter", () => {
  it("leaves paused projects and their tickets off the board by default", async () => {
    const text = (await open()).textContent ?? "";
    expect(text).toContain("widgets");
    expect(text).not.toContain("toy-paused");
    expect(text).not.toContain("Toy ticket");
  });

  it("draws them under ?show=all", async () => {
    const text = (await open("/overview?show=all")).textContent ?? "";
    expect(text).toContain("toy-paused");
    expect(text).toContain("Toy ticket");
  });
});
