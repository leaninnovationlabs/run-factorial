// @vitest-environment happy-dom
/** A run of tool calls as one row: the call at work while one runs, a count once all are done. */

import { useState } from "react";
import { afterEach, describe, expect, test } from "vitest";
import type { ChatItem } from "../../../api/types";
import { click, type Mounted, mount } from "../../../testDom";
import { chatItem } from "../testFakes";
import { ToolGroup } from "./ToolGroup";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
});

const READ = {
  call_id: "c1",
  name: "factorial_ticket_get",
  input: { ticket: "acme/api#212" },
  status: "done",
  ok: true,
  summary: "ticket acme/api#212\n{}",
};
const LIST = { call_id: "c2", name: "factorial_tickets_list", input: {}, status: "done", ok: true };
const MISSING = {
  call_id: "c3",
  name: "factorial_ticket_get",
  input: { ticket: "acme/api#999" },
  status: "done",
  ok: false,
  summary: "no ticket acme/api#999\nValid: acme/api#212",
};

/** The group as the stream holds it: open or shut is the stream's, not the row's. */
function Held({ calls }: { calls: ChatItem[] }) {
  const [open, setOpen] = useState(false);
  return <ToolGroup calls={calls} open={open} onToggle={() => setOpen((shown) => !shown)} />;
}

async function render(calls: Record<string, unknown>[]) {
  view = await mount(
    <Held calls={calls.map((payload, at) => chatItem(at + 1, at + 1, "tool", payload))} />,
  );
  const summary = view.container.querySelector("button");
  if (!summary) throw new Error("no summary");
  return { root: view.container, summary };
}

describe("ToolGroup", () => {
  test("a finished run is counted and collapsed, and opens to each call's line", async () => {
    const { root, summary } = await render([READ, LIST]);
    expect(summary.textContent).toBe("Used 2 tools");
    expect(summary.getAttribute("aria-expanded")).toBe("false");
    expect(root.textContent).not.toContain("Read ticket acme/api#212");

    await click(summary);
    expect(summary.getAttribute("aria-expanded")).toBe("true");
    const lines = [...root.querySelectorAll("li")].map((line) => line.textContent);
    expect(lines).toEqual(["Read ticket acme/api#212", "List tickets"]);

    await click(summary);
    expect(root.querySelector("li")).toBeNull();
  });

  test("one call is one tool", async () => {
    const { summary } = await render([READ]);
    expect(summary.textContent).toBe("Used 1 tool");
  });

  test("a failure is counted on the collapsed row, and its error is on its line", async () => {
    const { root, summary } = await render([READ, MISSING]);
    expect(summary.textContent).toBe("Used 2 tools (1 failed)");
    expect(summary.querySelector(".text-danger")?.textContent).toContain("1 failed");
    expect(summary.querySelector('[data-glyph="status-error"]')).not.toBeNull();
    expect(summary.querySelector('[data-glyph="check"]')).toBeNull();

    await click(summary);
    expect(root.textContent).toContain("no ticket acme/api#999");
    expect(root.textContent).not.toContain("Valid: acme/api#212");
  });

  test("a stopped call is counted, and the row does not say the run went well", async () => {
    const { summary } = await render([READ, { ...LIST, status: "stopped", ok: undefined }]);
    expect(summary.textContent).toBe("Used 2 tools (1 stopped)");
    expect(summary.querySelector('[data-glyph="check"]')).toBeNull();
    expect(summary.querySelector('[data-glyph="close"]')).not.toBeNull();
  });

  test("a failure stays counted while a later call runs", async () => {
    const { summary } = await render([
      MISSING,
      { ...LIST, call_id: "c4", status: "running", ok: undefined },
    ]);
    expect(summary.textContent).toBe("Listing tickets… (1 failed)");
    expect(summary.querySelector(".text-danger")?.textContent).toContain("1 failed");
  });

  test("while a call runs, the row says what it is doing", async () => {
    const { summary } = await render([
      READ,
      { ...MISSING, call_id: "c4", status: "running", ok: undefined, summary: undefined },
    ]);
    expect(summary.textContent).toBe("Reading ticket acme/api#999…");
    expect(summary.querySelector('[data-glyph="check"]')).toBeNull();
  });

  test("a running call with no verb it knows is named as it is", async () => {
    const { summary } = await render([
      { call_id: "c1", name: "factorial_future_thing", status: "running" },
    ]);
    expect(summary.textContent).toBe("future thing…");
  });
});
