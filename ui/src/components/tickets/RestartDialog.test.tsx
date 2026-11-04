// @vitest-environment happy-dom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { RequestResult, RestartPreview } from "../../api/types";
import { byText, click, mount, settle } from "../../testDom";
import { RestartDialog } from "./RestartDialog";

const PINNED = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0";
const LATEST = "f0e1d2c3b4a5968778695a4b3c2d1e0f9a8b7c6d";
const TICKET = { id: 5204, project_id: 9041, external_key: "204" };

function preview(over: Partial<RestartPreview> = {}): RestartPreview {
  return {
    current: { process_version_id: 17, sha: PINNED },
    target: { process_version_id: 18, sha: LATEST },
    current_pin: false,
    live_run: { step: "impl", attempt: 2, kind: "agent" },
    parked_run: null,
    close_prs: [
      {
        number: 241,
        url: "https://github.com/northwind/harbor-invoices/pull/241",
        head: "impl/204-remit-to",
        base: "spec/204-remit-to",
      },
    ],
    delete_branches: ["impl/204-remit-to", "spec/204-remit-to"],
    merged_outside: [],
    blockers: [],
    ...over,
  };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** The API as the dialog meets it: the preview, and the restart's answer. Returns every body the
 *  restart was posted with. */
function serve(
  shown: RestartPreview,
  answer: () => Response = () => json({ status: "requested", landed: true }, 202),
) {
  const posted: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/restart/preview")) return json(shown);
      if (url.endsWith("/restart")) {
        posted.push(JSON.parse(String(init?.body)));
        return answer();
      }
      return json({});
    }),
  );
  return posted;
}

async function open(onRestarted: (result: RequestResult) => void = () => {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = await mount(
    <QueryClientProvider client={qc}>
      <RestartDialog ticket={TICKET} onClose={() => {}} onRestarted={onRestarted} />
    </QueryClientProvider>,
  );
  await settle();
  return view;
}

const dialog = () => document.querySelector<HTMLElement>('[role="dialog"]');
const confirm = () => byText(document.body, "button", "Restart ticket");

afterEach(() => vi.unstubAllGlobals());

describe("RestartDialog", () => {
  it("names both commits and everything the restart removes", async () => {
    serve(preview({ parked_run: { step: "plan", attempt: 1, kind: "agent" } }));
    const view = await open();
    expect(dialog()?.getAttribute("aria-modal")).toBe("true");
    expect(dialog()?.querySelector("h1")?.textContent).toBe("Restart #204 on the latest process?");
    const text = dialog()?.textContent ?? "";
    expect(text).toContain("From a1b2c3d to f0e1d2c");
    expect(text).toContain("impl attempt 2");
    expect(text).toContain("The paused session this closesplan attempt 1");
    const pr = dialog()?.querySelector(
      'a[href="https://github.com/northwind/harbor-invoices/pull/241"]',
    );
    expect(pr?.textContent).toBe("#241");
    const items = [...(dialog()?.querySelectorAll("li") ?? [])].map((li) => li.textContent);
    expect(items).toEqual(expect.arrayContaining(["impl/204-remit-to", "spec/204-remit-to"]));
    expect(confirm()?.getAttribute("aria-disabled")).not.toBe("true");
    await view.unmount();
  });

  it("disables Restart ticket while a blocker stands, and says why", async () => {
    const posted = serve(
      preview({
        blockers: [{ reason: "issue_closed", detail: "issue #204 is closed: reopen it first" }],
      }),
    );
    const view = await open();
    expect(confirm()?.getAttribute("aria-disabled")).toBe("true");
    expect(dialog()?.textContent).toContain("issue #204 is closed: reopen it first");
    await click(confirm());
    await settle();
    expect(posted).toEqual([]);
    await view.unmount();
  });

  it("asks before repeating merged work, and restarts with it accepted", async () => {
    const posted = serve(
      preview({
        merged_outside: [
          {
            number: 239,
            url: "https://github.com/northwind/harbor-invoices/pull/239",
            head: "spec/204-remit-to",
            base: "main",
          },
        ],
      }),
    );
    const view = await open();
    const text = dialog()?.textContent ?? "";
    expect(text).toContain("Already merged");
    expect(
      dialog()?.querySelector('a[href="https://github.com/northwind/harbor-invoices/pull/239"]')
        ?.textContent,
    ).toBe("#239");
    expect(confirm()?.getAttribute("aria-disabled")).toBe("true");
    await click(confirm());
    await settle();
    expect(posted).toEqual([]);

    await click(dialog()?.querySelector<HTMLElement>('input[type="checkbox"]') ?? null);
    await settle();
    expect(confirm()?.getAttribute("aria-disabled")).not.toBe("true");
    await click(confirm());
    await settle(20);
    expect(posted).toEqual([{ process_version_id: 18, allow_merged: true }]);
    await view.unmount();
  });

  it("will not restart a ticket already on the latest version of its process", async () => {
    const posted = serve(
      preview({ current_pin: true, target: { process_version_id: 17, sha: PINNED } }),
    );
    const view = await open();
    expect(confirm()?.getAttribute("aria-disabled")).toBe("true");
    expect(dialog()?.textContent).toContain(
      "The ticket already uses the latest version of its process.",
    );
    await click(confirm());
    await settle();
    expect(posted).toEqual([]);
    await view.unmount();
  });

  it("restarts on the snapshot it showed, and never asks to restart on the current one", async () => {
    const posted = serve(preview());
    const onRestarted = vi.fn();
    const view = await open(onRestarted);
    await click(confirm());
    await settle(20);
    expect(posted).toEqual([{ process_version_id: 18 }]);
    expect(onRestarted).toHaveBeenCalledExactlyOnceWith({ status: "requested", landed: true });
    await view.unmount();
  });

  it("keeps a refusal in the dialog, beside the button that asked", async () => {
    serve(preview(), () =>
      json({ detail: "the lane moved to 0a1b2c3; reload and try again" }, 409),
    );
    const onRestarted = vi.fn();
    const view = await open(onRestarted);
    await click(confirm());
    await settle(20);
    expect(dialog()?.querySelector('[role="alert"]')?.textContent).toContain(
      "the lane moved to 0a1b2c3",
    );
    expect(onRestarted).not.toHaveBeenCalled();
    await view.unmount();
  });
});
