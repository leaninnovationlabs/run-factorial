// @vitest-environment happy-dom
/** A notice about a turn: what happened, and the one thing to do about it. */

import { afterEach, describe, expect, test, vi } from "vitest";
import { MEMBER_PERMISSIONS } from "../../../api/organization.fixture";
import { byText, click, type Mounted } from "../../../testDom";
import { chatItem, mountInAssistant } from "../testFakes";
import { Notice } from "./Notice";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
});

const ASKED = chatItem(
  10,
  10,
  "user_text",
  { text: "which tickets are blocked?", page: "/overview" },
  {
    turn_id: 9,
  },
);

async function render(
  payload: Record<string, unknown>,
  turnId: number | null = 9,
  permissions?: string[],
) {
  const onRetry = vi.fn();
  const item = chatItem(12, 12, "notice", payload, { turn_id: turnId });
  const mounted = await mountInAssistant(
    <Notice item={item} items={[ASKED, item]} onRetry={onRetry} />,
    { permissions },
  );
  view = mounted.view;
  return { root: mounted.view.container, onRetry, where: mounted.where };
}

describe("Notice", () => {
  test("Notice for a failed turn offers Retry, which resends the turn's message", async () => {
    const { root, onRetry } = await render({
      text: "The model is rate limited. Wait, then retry.",
      reason: "rate_limit",
    });
    expect(root.textContent).toContain("The model is rate limited. Wait, then retry.");
    await click(byText(root, "button", "Retry"));
    expect(onRetry).toHaveBeenCalledExactlyOnceWith("which tickets are blocked?", "/overview");
  });

  test("a turn the assistant lost to a restart can be sent again", async () => {
    const { root } = await render({ text: "The assistant restarted. Send your message again." });
    expect(byText(root, "button", "Retry")).not.toBeNull();
  });

  test("a rejected credential links to the assistant settings instead", async () => {
    const { root, where } = await render({
      text: "The Claude credential was rejected.",
      reason: "auth",
    });
    expect(byText(root, "button", "Retry")).toBeNull();
    await click(byText(root, "a", "Open the assistant settings"));
    expect(where()).toBe("/settings");
  });

  test("a rejected credential links nowhere for a reader who cannot change the settings", async () => {
    const { root } = await render(
      { text: "The Claude credential was rejected.", reason: "auth" },
      9,
      MEMBER_PERMISSIONS,
    );
    expect(root.textContent).toContain("The Claude credential was rejected.");
    expect(root.querySelector("a, button")).toBeNull();
  });

  test("a limit, a refusal, or a proposal's outcome offers nothing to press", async () => {
    for (const payload of [
      { text: "Stopped at the per-turn limit.", reason: "budget" },
      { text: "The model declined this request.", reason: "refusal" },
    ]) {
      const { root } = await render(payload);
      expect(root.querySelector("a, button")).toBeNull();
      await view?.unmount();
      view = null;
    }
    const { root } = await render(
      { text: "Approved: Retry a failed run now for acme/api#212." },
      null,
    );
    expect(root.querySelector("a, button")).toBeNull();
  });
});
