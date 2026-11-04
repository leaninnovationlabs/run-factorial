// @vitest-environment happy-dom
/**
 * The error boxes say the plain message first, then the next step the API names, and fold the
 * technical text behind Details: an operator opens it, and nobody else reads a status line.
 */
import { afterEach, describe, expect, it } from "vitest";
import { ApiError, SERVER_FAILED } from "../../api/client";
import { click, type Mounted, mount } from "../../testDom";
import { ActionError, ErrorMessage } from "./messages";

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
