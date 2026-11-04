import { describe, expect, it } from "vitest";
import type { ProjectPlace } from "../project";
import { tabTargets } from "./TabBar";

const inProject = (area: ProjectPlace["area"]): ProjectPlace => ({
  projectId: 7,
  area,
  section: null,
});

describe("tabTargets", () => {
  it("has no Tickets outside a project, and sends the Assistant to its page", () => {
    expect(tabTargets(null, "/overview", true)).toEqual({
      tickets: null,
      assistant: { kind: "page", isCurrent: false },
    });
    expect(tabTargets(null, "/assistant", true).assistant).toEqual({
      kind: "page",
      isCurrent: true,
    });
  });

  it("sends Tickets to the project's list inside one, and opens the Assistant over the page", () => {
    expect(tabTargets(inProject("dashboard"), "/projects/7", true)).toEqual({
      tickets: { to: "/projects/7/tickets", isCurrent: false },
      assistant: { kind: "drawer" },
    });
    expect(tabTargets(inProject("tickets"), "/projects/7/tickets", true).tickets?.isCurrent).toBe(
      true,
    );
  });

  it("lights Tickets on a ticket's page once its project is known", () => {
    expect(tabTargets(null, "/tickets/41", true).tickets).toBeNull();
    expect(tabTargets(inProject("tickets"), "/tickets/41", true).tickets).toEqual({
      to: "/projects/7/tickets",
      isCurrent: true,
    });
  });

  it("leaves the Assistant out for a reader who may not use it", () => {
    expect(tabTargets(null, "/overview", false).assistant).toBeNull();
    expect(tabTargets(inProject("dashboard"), "/projects/7", false).assistant).toBeNull();
  });

  it("lights nothing on a page neither tab names", () => {
    const targets = tabTargets(inProject("configuration"), "/projects/7/configuration", true);
    expect(targets.tickets?.isCurrent).toBe(false);
  });
});
