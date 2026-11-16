// @vitest-environment happy-dom
/** The waiting-gate message's actions: the pull request, the preview, and Request changes, or
 *  Close and exit once the pull request closed without a merge; and which messages a scope draws. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { TicketDetail } from "../../api/types";
import { click, type Mounted, mount, settle } from "../../testDom";
import { TicketMessages } from "./TicketMessages";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
});

const TICKET = { id: 7 } as unknown as TicketDetail;

type Props = Parameters<typeof TicketMessages>[0];

async function show(review: Props["review"], extra: Partial<Props> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  view = await mount(
    <QueryClientProvider client={qc}>
      <TicketMessages
        ticket={TICKET}
        condition="waiting_gate"
        failing={false}
        gatePr="42"
        repo="acme/app"
        onRequeue={() => {}}
        review={review}
        {...extra}
      />
    </QueryClientProvider>,
  );
  await settle();
  return [...document.querySelectorAll<HTMLElement>("a, button")];
}

const named = (controls: HTMLElement[], text: string) =>
  controls.find((control) => control.textContent?.trim() === text);

describe("the waiting-gate message", () => {
  it("offers the preview and Request changes beside the pull request", async () => {
    const onOpenPreview = vi.fn();
    const onRequestChanges = vi.fn();
    const controls = await show({ onOpenPreview, onRequestChanges });

    expect(named(controls, "View pull request")).toBeDefined();
    await click(named(controls, "Open preview") as HTMLElement);
    await click(named(controls, "Request changes") as HTMLElement);

    expect(onOpenPreview).toHaveBeenCalledOnce();
    expect(onRequestChanges).toHaveBeenCalledOnce();
  });

  it("offers no preview for a step without one, and nothing without a review", async () => {
    const without = await show({ onRequestChanges: () => {} });
    expect(named(without, "Open preview")).toBeUndefined();
    expect(named(without, "Request changes")).toBeDefined();
    await view?.unmount();

    const none = await show(null);
    expect(named(none, "Request changes")).toBeUndefined();
  });
});

describe("a gate whose pull request closed without a merge", () => {
  it("says the ticket is held, and offers Close and exit instead of Request changes", async () => {
    const onCloseAndExit = vi.fn();
    const controls = await show(
      { onRequestChanges: () => {}, onRedo: () => {}, redoStep: "plan" },
      { gateClosed: true, onCloseAndExit },
    );
    const message = document.querySelector("section");

    expect(message?.querySelector("h3")?.textContent).toBe("Pull request closed");
    expect(message?.textContent).toContain("Pull request #42 was closed without merging");
    expect(message?.getAttribute("data-appearance")).toBe("warning");
    expect(named(controls, "View pull request")).toBeDefined();
    expect(named(controls, "Redo plan")).toBeDefined();
    expect(named(controls, "Request changes")).toBeUndefined();
    await click(named(controls, "Close and exit") as HTMLElement);
    expect(onCloseAndExit).toHaveBeenCalledOnce();
  });

  it("names a merge request on GitLab", async () => {
    await show(null, {
      gateClosed: true,
      onCloseAndExit: () => {},
      forge: { kind: "gitlab", origin: "https://gitlab.test" },
    });
    expect(document.querySelector("section h3")?.textContent).toBe("Merge request closed");
  });
});

describe("a message's scope", () => {
  it.each([
    ["ticket", "Couldn't check the ticket", "Waiting for a review"],
    ["step", "Waiting for a review", "Couldn't check the ticket"],
  ] as const)("draws only the %s's messages", async (scope, shown, left) => {
    // Both at once: a gate waits on a review while the ticket's latest state can't be read.
    await show({ onRequestChanges: () => {} }, { failing: true, scope });
    expect(document.body.textContent).toContain(shown);
    expect(document.body.textContent).not.toContain(left);
  });
});
