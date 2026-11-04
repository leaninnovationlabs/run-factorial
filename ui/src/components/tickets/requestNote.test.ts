import { describe, expect, it } from "vitest";
import { requestNote } from "./requestNote";

describe("requestNote", () => {
  it("says nothing before the first click, or once the pass has landed", () => {
    // Landed, the refetch already shows what the pass did; a note would only repeat it.
    expect(requestNote("exit", undefined)).toBeNull();
    expect(requestNote("exit", { status: "requested", landed: true })).toBeNull();
  });

  it("says the request is queued when its pass has not landed", () => {
    // A failing ticket's request wakes nothing, and a wait can run out: either way the chip is
    // back to "exit", and without this a second click files a second request.
    const note = requestNote("exit", { status: "requested", landed: false });
    expect(note).toBe("Exit requested. This page updates when it takes effect.");
  });
});
