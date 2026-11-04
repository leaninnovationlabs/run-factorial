import { describe, expect, it } from "vitest";
import type { OverviewResponse, Runtime } from "../../api/types";
import { JUMP_LIMIT, jumpMatches } from "./jump";

const OVERVIEW = {
  projects: [
    { id: 1, name: "widgets" },
    { id: 2, name: "payments-api" },
  ],
  tickets: [
    { ticket_id: 11, project_id: 1, external_key: "acme/widgets#118", title: "Retry the webhook" },
    { ticket_id: 12, project_id: 2, external_key: "acme/payments#1180", title: "Refund rounding" },
    { ticket_id: 13, project_id: 1, external_key: "acme/widgets#7", title: null },
  ],
  done_days: 7,
} as unknown as OverviewResponse;

const RUNTIMES = [{ slug: "codex-team", name: "Codex team" }] as Runtime[];

const jump = (query: string) => jumpMatches(query, OVERVIEW, RUNTIMES);

describe("jumpMatches", () => {
  it("answers nothing until something is typed", () => {
    expect(jump("")).toEqual([]);
    expect(jump("   ")).toEqual([]);
  });

  it("puts the ticket whose number IS the query first, ahead of numbers that merely contain it", () => {
    // `118` is in `1180` too; the reader who typed 118 meant #118.
    expect(jump("118").map((answer) => answer.to)).toEqual(["/tickets/11", "/tickets/12"]);
    expect(jump("#118")[0]?.to).toBe("/tickets/11");
  });

  it("finds a ticket by the words of its title, and names its project", () => {
    expect(jump("refund")).toEqual([
      { kind: "ticket", label: "#1180 Refund rounding", detail: "payments-api", to: "/tickets/12" },
    ]);
  });

  it("wants every word, in any order", () => {
    expect(jump("webhook retry").map((answer) => answer.to)).toEqual(["/tickets/11"]);
    expect(jump("webhook refund")).toEqual([]);
  });

  it("finds projects and runtimes by name, runtimes by slug too", () => {
    expect(jump("payments")).toContainEqual(
      expect.objectContaining({ kind: "project", to: "/projects/2" }),
    );
    expect(jump("codex-team")).toEqual([
      {
        kind: "runtime",
        label: "Codex team",
        detail: "Runtime · codex-team",
        to: "/runtimes/codex-team",
      },
    ]);
  });

  it("lists no more than a glance's worth", () => {
    const many = {
      ...OVERVIEW,
      tickets: Array.from({ length: 30 }, (_, index) => ({
        ticket_id: 100 + index,
        project_id: 1,
        external_key: `acme/widgets#${index}`,
        title: "Same words",
      })),
    } as unknown as OverviewResponse;
    expect(jumpMatches("same", many, [])).toHaveLength(JUMP_LIMIT);
  });

  it("answers from whatever has loaded, and nothing when nothing has", () => {
    expect(jumpMatches("widgets", undefined, undefined)).toEqual([]);
    expect(jumpMatches("codex", undefined, RUNTIMES)).toHaveLength(1);
  });
});
