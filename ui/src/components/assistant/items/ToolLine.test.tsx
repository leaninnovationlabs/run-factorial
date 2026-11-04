// @vitest-environment happy-dom
/** A tool call as one compact line, as an open tool group lists it. */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, test } from "vitest";
import { namesCard, ToolLine } from "./ToolLine";

function text(payload: Record<string, unknown>): string {
  const host = document.createElement("div");
  host.innerHTML = renderToStaticMarkup(<ToolLine payload={payload} />);
  return host.textContent ?? "";
}

describe("ToolLine", () => {
  test("a call whose result names a card is known by it", () => {
    expect(namesCard('chart card 41: 1 series\n{"card_item_id":41}')).toBe(true);
    expect(namesCard("ticket acme/api#212\n{}")).toBe(false);
    expect(namesCard(undefined)).toBe(false);
  });

  test("ToolLine names the read and what it read", () => {
    expect(
      text({
        call_id: "c1",
        name: "factorial_ticket_get",
        input: { ticket: "acme/api#212" },
        status: "done",
        ok: true,
        summary: "ticket acme/api#212\n{}",
      }),
    ).toBe("Read ticket acme/api#212");
    expect(
      text({ call_id: "c2", name: "factorial_tickets_list", input: {}, status: "running" }),
    ).toContain("List tickets");
  });

  test("ToolLine shows a tool's error on its line", () => {
    const line = text({
      call_id: "c1",
      name: "factorial_ticket_get",
      input: { ticket: "acme/api#999" },
      status: "done",
      ok: false,
      summary: "no ticket acme/api#999\nValid: acme/api#212",
    });
    expect(line).toContain("Read ticket acme/api#999");
    expect(line).toContain("no ticket acme/api#999");
  });

  test("ToolLine names a tool it does not know by its own name", () => {
    expect(text({ call_id: "c1", name: "factorial_future_thing", status: "done", ok: true })).toBe(
      "future thing",
    );
  });

  test("ToolLine names a restart as the proposal it records", () => {
    expect(
      text({
        call_id: "c1",
        name: "factorial_ticket_restart",
        input: { ticket: "acme/api#212" },
        status: "running",
      }),
    ).toBe("Propose a restart acme/api#212");
  });
});
