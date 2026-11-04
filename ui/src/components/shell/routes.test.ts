/**
 * How wide each page reads, and which fill the window. The shell owns this — a page cannot widen
 * past a container it does not own — so this is the only place it can be asserted.
 */

import { describe, expect, it } from "vitest";
import { routeFor } from "./routes";

const width = (path: string) => routeFor(path)?.width;

describe("routeFor", () => {
  it("gives the whole window to the pages that spend width on information", () => {
    for (const path of [
      "/overview",
      "/projects",
      "/projects/9041",
      "/projects/9041/tickets",
      "/projects/9041/configuration",
      "/projects/9041/configuration/secrets",
      "/tickets/204",
      "/runtimes",
    ]) {
      expect(width(path), path).toBe("full");
    }
  });

  it("keeps a form narrow, and is not fooled by the entity page sharing its prefix", () => {
    expect(width("/projects/new")).toBe("narrow");
    // Wider: its cards sit beside a summary.
    expect(width("/runtimes/new")).toBe("default");
    expect(width("/runtimes/codex-team")).toBe("default");
  });

  it("fills the window only where a region scrolls inside the page", () => {
    // The board and its list, and the ticket's activity: each ends at the bottom of the window.
    const filled = ["/overview", "/tickets/204", "/projects", "/runtimes/new"].filter(
      (path) => routeFor(path)?.fill,
    );
    expect(filled).toEqual(["/overview", "/tickets/204"]);
  });

  it("has no route for an address the app does not have, so the shell can say so", () => {
    expect(routeFor("/somewhere-nobody-has-built-yet")).toBeNull();
    expect(routeFor("/projects/9041/extra")).toBeNull();
    expect(routeFor("/projects/9041/configuration/secrets/more")).toBeNull();
  });
});
