// @vitest-environment happy-dom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { ApiError } from "../../api/client";
import { type Mounted, mount, settle } from "../../testDom";
import { ConnectionBanner, outage, UNREACHABLE_AFTER } from "./ConnectionBanner";

let view: Mounted | null = null;
afterEach(async () => {
  await view?.unmount();
  view = null;
});

describe("outage", () => {
  it("counts a read that got no answer, and not one the API answered no", () => {
    expect(outage(new ApiError(0, "cannot reach the API"))).toBe(true);
    expect(outage(new ApiError(502, "bad gateway"))).toBe(true);
    expect(outage(new TypeError("fetch failed"))).toBe(true);
    for (const status of [401, 404, 409, 422])
      expect(outage(new ApiError(status, "no"))).toBe(false);
  });
});

async function banner(client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  view = await mount(
    <QueryClientProvider client={client}>
      <ConnectionBanner />
    </QueryClientProvider>,
  );
  return { root: view.container, client };
}

const fail = (client: QueryClient, key: string) =>
  act(async () => {
    await client
      .fetchQuery({ queryKey: [key], queryFn: () => Promise.reject(new ApiError(0, "down")) })
      .catch(() => {});
  });

describe("ConnectionBanner", () => {
  it("says the API is unreachable after enough failed reads in a row, and clears on an answer", async () => {
    // A single blip is not an outage: every page polls, and one dropped read is ordinary.
    expect(UNREACHABLE_AFTER).toBeGreaterThan(1);
    const { root, client } = await banner();
    for (let index = 0; index < UNREACHABLE_AFTER - 1; index += 1) await fail(client, `q${index}`);
    expect(root.textContent).toBe("");
    await fail(client, "last");
    expect(root.textContent).toContain(
      "Factorial isn't responding, so this page may be out of date. Reconnecting.",
    );

    await act(async () => {
      await client.fetchQuery({ queryKey: ["ok"], queryFn: () => Promise.resolve(1) });
    });
    expect(root.textContent).toBe("");
  });

  it("offers a reload when a newer build took this tab's files away", async () => {
    const { root } = await banner();
    const failed = new Event("vite:preloadError", { cancelable: true });
    await act(async () => {
      window.dispatchEvent(failed);
    });
    await settle();
    // Not claimed: Vite then rejects the import, and the page's error boundary catches it.
    // Claimed, the import resolved to undefined and the page crashed reading it.
    expect(failed.defaultPrevented).toBe(false);
    expect(root.textContent).toContain("A new version of Factorial is available.");
    expect(root.querySelector("button")?.textContent).toBe("Reload");
  });
});
