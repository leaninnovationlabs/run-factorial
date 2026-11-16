import { describe, expect, it } from "vitest";
import { enter, inOrganization, isOrgless } from "./org";

const at = (pathname: string, search = "", hash = "") => ({ pathname, search, hash });

describe("entering a page", () => {
  it("reads the organization from its prefix and moves nothing", () => {
    expect(enter(at("/factorial/orgs/acme/tickets/7"), "default")).toEqual({
      org: "acme",
      redirect: null,
    });
    expect(enter(at("/factorial/orgs/acme"), null)).toEqual({ org: "acme", redirect: null });
  });

  it("moves a bare path under the last organization this browser used", () => {
    expect(enter(at("/factorial/tickets/7", "?terminal=3", "#log"), "acme")).toEqual({
      org: "acme",
      redirect: "/factorial/orgs/acme/tickets/7?terminal=3#log",
    });
  });

  it("sends /orgs without a usable slug to the organization's home", () => {
    expect(enter(at("/factorial/orgs/"), "acme")).toEqual({
      org: "acme",
      redirect: "/factorial/orgs/acme",
    });
    expect(enter(at("/factorial/orgs/%zz/overview"), null)).toEqual({
      org: "default",
      redirect: "/factorial/orgs/default",
    });
  });

  it("falls back to default with nothing remembered, or something no slug could be", () => {
    for (const home of ["/factorial", "/factorial/"]) {
      expect(enter(at(home), null)).toEqual({
        org: "default",
        redirect: "/factorial/orgs/default",
      });
    }
    expect(enter(at("/factorial/projects"), "../evil")).toEqual({
      org: "default",
      redirect: "/factorial/orgs/default/projects",
    });
  });

  it("leaves the paths that belong to no organization where they are", () => {
    expect(enter(at("/factorial/sign-in", "?next=/factorial/pages/3"), "acme")).toEqual({
      org: null,
      redirect: null,
    });
    expect(enter(at("/factorial/dev/catalogue"), "acme")).toEqual({ org: null, redirect: null });
    expect(enter(at("/factorial/dev/sample"), "acme")).toEqual({ org: null, redirect: null });
  });
});

describe("a router path outside the router", () => {
  it("carries the prefix and the organization, so a sign-in returns to the same page", () => {
    expect(inOrganization("acme", "/account/mcp")).toBe("/factorial/orgs/acme/account/mcp");
    expect(inOrganization("acme", "/")).toBe("/factorial/orgs/acme");
  });
});

describe("the pages in no organization", () => {
  it("leaves signup, onboarding, joining and an invite's link where they are", () => {
    for (const path of [
      "/factorial/signup",
      "/factorial/onboarding",
      "/factorial/join",
      "/factorial/invite/abc_DEF-1",
    ]) {
      expect(enter(at(path), "acme")).toEqual({ org: null, redirect: null });
    }
  });

  it("takes an invite path only with its token", () => {
    expect(isOrgless("/invite/abc")).toBe(true);
    expect(isOrgless("/invite/")).toBe(false);
    expect(isOrgless("/invite/abc/more")).toBe(false);
    expect(enter(at("/factorial/invite"), "acme").org).toBe("acme");
  });
});
