/**
 * The project ticket table's issue cell.
 *
 * Rendered with `renderToString` inside a `MemoryRouter` — `useRowLink` needs a router, and there
 * is no DOM in this test environment. Same shape as `factory-board/IssueList.test.tsx`.
 */

import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import type { TicketListItem } from "../../api/types";
import { NO_VALUE } from "../../format";
import { TicketTable } from "./TicketTable";

const TICKET: TicketListItem = {
  id: 41,
  external_key: "118",
  title: "Retry the webhook receiver",
  current_step: "spec",
  process_key: "standard",
  status: "running",
  latest_run_status: "running",
  park_reason: null,
  owner_at_admission: "operator",
  process_version_id: 1,
  attempts: 2,
  input_tokens: 41_900,
  output_tokens: 9200,
  cost_usd: "0.9400",
  cost_estimated: false,
  admitted_at: "2026-08-30T12:00:00Z",
  closed_at: null,
  quarantined_at: null,
  updated_at: "2026-08-30T12:30:00Z",
};

/** `renderToString` separates adjacent text nodes with `<!-- -->`. */
const plain = (html: string) => html.replaceAll("<!-- -->", "");

/** Each metric box's figure and its unit, addressed by the `data-metric-*` handles `Metric` puts
 *  on them rather than by the utility classes beside them — see `ui.test.tsx`, which reads the
 *  same markup the same way. */
const boxes = (html: string): string[][] =>
  html
    .split("<span data-metric-box")
    .slice(1)
    .map((chunk) => [
      chunk.match(/data-metric-value[^>]*>([^<]*)</)?.[1] ?? "",
      chunk.match(/data-metric-unit[^>]*>([^<]*)</)?.[1] ?? "",
    ]);

/** The invisible figures a box holds room for — what `figureSizers` bought. */
const sizers = (html: string): string[] =>
  [...html.matchAll(/<span aria-hidden="true" class="invisible[^"]*">([^<]*)</g)].map(
    (match) => match[1] as string,
  );

const render = (tickets: TicketListItem[]) =>
  plain(
    renderToString(
      <MemoryRouter>
        <TicketTable tickets={tickets} repo="acme/repo" isLoading={false} />
      </MemoryRouter>,
    ),
  );

describe("TicketTable", () => {
  it("shows the issue title under its number", () => {
    const html = render([TICKET]);
    expect(html).toContain("#118");
    expect(html).toContain("Retry the webhook receiver");
  });

  it("shows the number alone for a ticket with no stored title", () => {
    // Admitted before the observer stored titles, or already done when it shipped. The number is
    // what the row identified a ticket by before titles existed; a blank line would read as a bug.
    const html = render([{ ...TICKET, title: null }]);
    expect(html).toContain("#118");
    expect(html).not.toContain("Retry the webhook receiver");
  });

  it("reads a quarantined ticket's pill off the flag, not its running status", () => {
    // Quarantine is orthogonal to status (decision 31): this row's `status` is still "running",
    // and the lozenge must say it stopped after errors anyway.
    const html = render([{ ...TICKET, quarantined_at: "2026-09-04T10:00:00Z" }]);
    expect(html).toContain(">Stopped after errors<");
    expect(html).not.toContain(">Running<");
  });

  it("says Waiting for an idle-parked ticket and Interrupted for one a human stopped", () => {
    const parked = { ...TICKET, status: "blocked", latest_run_status: "interrupted" } as const;
    const idle = render([{ ...parked, park_reason: "idle" }]);
    expect(idle).toContain(">Waiting<");
    expect(idle).not.toContain(">Interrupted<");
    const stopped = render([{ ...parked, park_reason: "user" }]);
    expect(stopped).toContain(">Interrupted<");
    expect(stopped).not.toContain(">Waiting<");
  });

  it("boxes the row's spend the way the ticket page boxes its own", () => {
    const html = render([TICKET]);
    expect(boxes(html)).toEqual([
      ["41.9K", "in"],
      ["9.2K", "out"],
      ["0.94", "$"],
    ]);
  });

  it("writes the empty mark for a ticket that has spent nothing", () => {
    // An admitted ticket, or one whose only attempt was a gate: `0 in / 0 out` and `$0.00` are
    // three boxes saying what the empty mark says in one character.
    const html = render([{ ...TICKET, input_tokens: 0, output_tokens: 0, cost_usd: "0.0000" }]);
    expect(boxes(html)).toEqual([]);
    expect(html.split(`>${NO_VALUE}<`)).toHaveLength(3);
  });

  it("sizes every cost figure against the widest in the column", () => {
    // One `figureSizers` set for the whole table, not one per row: the cheap row holds room for
    // the expensive row's figure, which is what makes the two boxes a single width and lines the
    // column up on the digits a reader is comparing down it.
    const html = render([TICKET, { ...TICKET, id: 42, cost_usd: "112.5000" }]);
    expect(sizers(html).filter((figure) => figure === "112.50")).toHaveLength(2);
  });
});
