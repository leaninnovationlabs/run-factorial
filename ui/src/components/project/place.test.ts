import { describe, expect, it } from "vitest";
import {
  configurationHref,
  legacyTabHref,
  placeOf,
  projectHref,
  projectTicketsHref,
  sameAreaHref,
} from "./place";

describe("placeOf", () => {
  it("reads the project and the page from a project's own addresses", () => {
    expect(placeOf("/projects/12")).toEqual({
      kind: "project",
      place: { projectId: 12, area: "dashboard", section: null },
    });
    expect(placeOf("/projects/12/tickets")).toEqual({
      kind: "project",
      place: { projectId: 12, area: "tickets", section: null },
    });
    expect(placeOf("/projects/12/configuration")).toEqual({
      kind: "project",
      place: { projectId: 12, area: "configuration", section: null },
    });
    expect(placeOf("/projects/12/configuration/labels")).toEqual({
      kind: "project",
      place: { projectId: 12, area: "configuration", section: "labels" },
    });
  });

  it("names the ticket on a ticket's page, whose project the address does not carry", () => {
    expect(placeOf("/tickets/204")).toEqual({ kind: "ticket", ticketId: 204 });
    expect(placeOf("/tickets/nope")).toBeNull();
  });

  it("is in no project on the organization's pages and the new-project form", () => {
    for (const path of [
      "/overview",
      "/projects",
      "/projects/new",
      "/runtimes/codex",
      "/settings",
    ]) {
      expect(placeOf(path), path).toBeNull();
    }
  });
});

describe("the project's links", () => {
  it("write every page of a project from its id", () => {
    expect(projectHref(7)).toBe("/projects/7");
    expect(projectTicketsHref(7)).toBe("/projects/7/tickets");
    expect(configurationHref(7)).toBe("/projects/7/configuration");
    expect(configurationHref(7, "process")).toBe("/projects/7/configuration/process");
  });

  it("switch projects onto the same page, never onto a section the other may not have", () => {
    expect(sameAreaHref(8, "dashboard")).toBe("/projects/8");
    expect(sameAreaHref(8, "tickets")).toBe("/projects/8/tickets");
    expect(sameAreaHref(8, "configuration")).toBe("/projects/8/configuration");
  });
});

describe("an address from before a project had pages", () => {
  it("goes to the page its tab named, and the editor to the process page", () => {
    expect(legacyTabHref("/projects/7", "tickets")).toBe("/projects/7/tickets");
    expect(legacyTabHref("/projects/7", "configuration")).toBe("/projects/7/configuration");
    expect(legacyTabHref("/projects/7", "configuration", "process")).toBe(
      "/projects/7/configuration/process",
    );
  });

  it("stays where it is when the tab is the dashboard, or names no page", () => {
    expect(legacyTabHref("/projects/7", "dashboard")).toBeNull();
    expect(legacyTabHref("/projects/7", null)).toBeNull();
    expect(legacyTabHref("/tickets/7", "tickets")).toBeNull();
  });
});
