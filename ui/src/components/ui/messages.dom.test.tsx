// @vitest-environment happy-dom
/**
 * The error boxes say the plain message first, then the next step the API names, and fold the
 * technical text behind Details: an operator opens it, and nobody else reads a status line. An
 * action's success can be closed once read, and the next one shows again.
 */
import { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { ApiError, SERVER_FAILED } from "../../api/client";
import { click, type Mounted, mount } from "../../testDom";
import { ActionError, ErrorMessage, SectionMessage, useDismissible } from "./messages";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
});

function toggle(): HTMLButtonElement | null {
  return document.querySelector("button[aria-expanded]");
}

function details(): HTMLElement | null {
  const id = toggle()?.getAttribute("aria-controls");
  return id ? document.getElementById(id) : null;
}

const failed = new ApiError(502, SERVER_FAILED, null, { detail: "GitHub answered 404" });

describe.each([
  ["ErrorMessage", (error: unknown) => <ErrorMessage error={error} />],
  ["ActionError", (error: unknown) => <ActionError error={error} />],
])("%s", (_name, draw) => {
  it("keeps the technical text folded until Show details is pressed", async () => {
    view = await mount(draw(failed));
    expect(document.body.textContent).toContain(SERVER_FAILED);
    expect(toggle()?.textContent).toBe("Show details");
    expect(details()?.textContent).toBe("GitHub answered 404");
    expect(details()?.hidden).toBe(true);
    await click(toggle());
    expect(details()?.hidden).toBe(false);
    expect(toggle()?.getAttribute("aria-expanded")).toBe("true");
    expect(toggle()?.textContent).toBe("Hide details");
  });

  it("offers no Details for a refusal whose message is the server's own", async () => {
    view = await mount(draw(new ApiError(409, "the project is paused")));
    expect(document.body.textContent).toContain("the project is paused");
    expect(toggle()).toBeNull();
  });

  it("says the next step the API names after the message", async () => {
    const refusal = new ApiError(409, "No process yet.", "not_ready", {
      hint: "Sync one from the repository.",
    });
    view = await mount(draw(refusal));
    expect(document.body.textContent).toMatch(/No process yet\.\s*Sync one from the repository\./);
  });
});

describe("a success that can be closed", () => {
  const dismiss = () => document.querySelector<HTMLButtonElement>('button[aria-label="Dismiss"]');

  it("draws no close button unless the message is given one", async () => {
    view = await mount(<SectionMessage appearance="success" title="Synced" />);
    expect(dismiss()).toBeNull();
  });

  /** A page that runs an action again and again, its outcome a new object each time. */
  function Saving() {
    const [result, setResult] = useState<{ n: number } | null>(null);
    const [shown, close] = useDismissible(result);
    return (
      <>
        <button type="button" onClick={() => setResult({ n: (result?.n ?? 0) + 1 })}>
          Save
        </button>
        {shown && (
          <SectionMessage appearance="success" title={`Saved ${shown.n}`} onDismiss={close} />
        )}
      </>
    );
  }

  const save = () => document.querySelector<HTMLButtonElement>("button:not([aria-label])");

  it("closes on its button, and shows again for the next outcome", async () => {
    view = await mount(<Saving />);
    await click(save());
    expect(document.body.textContent).toContain("Saved 1");
    await click(dismiss());
    expect(document.body.textContent).not.toContain("Saved");
    await click(save());
    expect(document.body.textContent).toContain("Saved 2");
    expect(dismiss()).not.toBeNull();
  });
});
