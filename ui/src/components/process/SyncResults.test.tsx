// @vitest-environment happy-dom
/**
 * A sync's outcome: the success closes once read, a warning under it stays, and with nothing left
 * nothing is drawn, so the messages around it can hide when they are empty.
 */
import { afterEach, describe, expect, it } from "vitest";
import type { ProcessSync } from "../../api/types";
import { click, type Mounted, mount } from "../../testDom";
import { useDismissible } from "../ui";
import { SyncResults } from "./SyncResults";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
});

const sync = (warnings: string[] = []): ProcessSync => ({
  sha: "a1b2c3d4e5",
  results: [{ process: "standard", outcome: "created" }] as ProcessSync["results"],
  agent_warnings: warnings,
  default_process_moved: null,
});

/** The closing kept where the result is, as the section and the editor keep it. */
function Held({ result }: { result: ProcessSync }) {
  const [shown, close] = useDismissible(result);
  return <SyncResults result={result} shown={shown !== undefined} onDismiss={close} />;
}

const dismiss = () => document.querySelector<HTMLButtonElement>('button[aria-label="Dismiss"]');

describe("SyncResults", () => {
  it("closes the success and keeps the warning under it", async () => {
    view = await mount(<Held result={sync(["builder: no agent.yaml"])} />);
    expect(document.body.textContent).toContain("Synced from");
    await click(dismiss());
    expect(document.body.textContent).not.toContain("Synced from");
    expect(document.body.textContent).toContain("Some agents need attention");
    expect(dismiss()).toBeNull();
  });

  it("draws nothing once a clean sync's success is closed", async () => {
    view = await mount(
      <div data-holder>
        <Held result={sync()} />
      </div>,
    );
    await click(dismiss());
    expect(document.querySelector("[data-holder]")?.childElementCount).toBe(0);
  });
});
