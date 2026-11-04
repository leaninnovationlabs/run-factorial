import { describe, expect, it } from "vitest";
import type { RefreshResult, TicketDetail } from "../../api/types";
import { formatClock } from "../../format";
import { refreshNote } from "./refreshNote";

const AT = "2026-09-13T10:04:07Z";

function result(landed: boolean, consecutive_failures = 0): RefreshResult {
  return {
    landed,
    requested_at: AT,
    ticket: { id: 5204, consecutive_failures } as TicketDetail,
  };
}

describe("refreshNote", () => {
  it("says nothing before the first click", () => {
    expect(refreshNote({})).toBeNull();
  });

  it("says nothing while a request is quick, and that it waits once it is not", () => {
    // The button's own spinner covers the first seconds; after that the wait needs words.
    expect(refreshNote({}, false)).toBeNull();
    expect(refreshNote({}, true)).toBe("refreshing…");
  });

  it("names the time of the request the pass answered", () => {
    expect(refreshNote({ data: result(true) })).toBe(`updated at ${formatClock(AT)}`);
  });

  it("says a pass that raised failed, rather than calling it a check", () => {
    // The pass released the ticket, so it landed, but it recorded a failure on the way out.
    const note = refreshNote({ data: result(true, 1) });
    expect(note).toBe(`refresh at ${formatClock(AT)} failed`);
  });

  it("says the request is queued when the API stopped waiting first", () => {
    // 202: a fleet tick held the ticket, or no worker was listening. The pass may still come,
    // and the page's own polling shows it when it does.
    expect(refreshNote({ data: result(false) })).toBe(`refresh sent at ${formatClock(AT)}`);
  });

  it("keeps every note short enough to share the facts row", () => {
    const notes = [
      refreshNote({}, true),
      refreshNote({ data: result(true) }),
      refreshNote({ data: result(true, 1) }),
      refreshNote({ data: result(false) }),
    ];
    // The clock's length is the locale's; the words around it are ours.
    for (const note of notes) {
      expect((note ?? "").replace(formatClock(AT), "").length, note ?? "").toBeLessThanOrEqual(20);
    }
  });
});
