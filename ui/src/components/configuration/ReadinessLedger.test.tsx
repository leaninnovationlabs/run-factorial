/**
 * The checklist's two sources, and the overview that draws them.
 *
 * Its rows arrive on two payloads — the project's own, and the repo-side probes nobody runs by
 * arriving — so the merge has to hold the reader's place while the second is in flight, and say
 * plainly when it has not been asked for.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import type { ProjectDetail, ProjectReadiness, ReadinessItem, ReadinessKey } from "../../api/types";
import { ConfigurationLedger } from "./ReadinessLedger";
import { CHECKING, ledgerRows, retiredSection, sectionRows, UNCHECKED } from "./readiness";

const item = (
  key: ReadinessKey,
  state: ReadinessItem["state"],
  summary: string,
): ReadinessItem => ({
  key,
  process: null,
  runtime_slug: null,
  state,
  summary,
  detail: null,
  remedy: null,
  link: null,
});

const CORE: ReadinessItem[] = [
  item("ticket_source", "ok", "acme/app"),
  item("process_config", "ok", "adlc-simple"),
  item("secrets", "ok", "every declared secret is stored"),
];

const PROBED: ProjectReadiness = {
  readiness: [
    item("app_access", "ok", "installed on acme"),
    item("labels", "warn", "4 missing"),
    item("branch_protection", "ok", "main · 1 approval"),
  ],
  warnings: [],
};

describe("ledgerRows", () => {
  it("holds a place for the gate approvers row after the other probe rows", () => {
    const rows = ledgerRows([], undefined, [
      "app_access",
      "config_branch",
      "labels",
      "branch_protection",
      "gate_approvers",
    ]);
    expect(rows.map((row) => row.key)).toEqual([
      "app_access",
      "branch_protection",
      "gate_approvers",
      "labels",
    ]);
  });

  it("stands in for the four rows the probes always answer while they are in flight", () => {
    const rows = ledgerRows(CORE, undefined);

    expect(rows.map((row) => row.key)).toEqual([
      "app_access",
      "config_branch_rules",
      "process_config",
      "ticket_source",
      "labels",
      "secrets",
    ]);
    expect(rows.filter((row) => row.summary === CHECKING)).toHaveLength(3);
    // The branch's placeholder says nothing about the config, so the process row keeps its state.
    expect(rows.find((row) => row.key === "process_config")?.state).toBe("ok");
  });

  it("says nobody asked, where nobody has", () => {
    const rows = ledgerRows(CORE, undefined, undefined, UNCHECKED);
    expect(rows.filter((row) => row.summary === UNCHECKED)).toHaveLength(3);
  });

  it("takes the answers once they land, placeholders and all", () => {
    const rows = ledgerRows(CORE, PROBED);

    expect(rows.map((row) => row.key)).toEqual([
      "app_access",
      "branch_protection",
      "process_config",
      "ticket_source",
      "labels",
      "secrets",
    ]);
    expect(rows.some((row) => row.summary === CHECKING)).toBe(false);
    expect(rows.find((row) => row.key === "labels")?.state).toBe("warn");
  });

  it("folds a warning config branch into the process config row, and drops its own row", () => {
    const rows = ledgerRows(CORE, {
      ...PROBED,
      readiness: [...PROBED.readiness, item("config_branch", "warn", "unknown head eeeeeee")],
    });
    expect(rows.map((row) => row.key)).not.toContain("config_branch");
    const process = rows.find((row) => row.key === "process_config");
    expect(process?.state).toBe("warn");
    expect(process?.summary).toBe("adlc-simple");
  });

  it("does not let an unchecked branch make a ready process look unchecked", () => {
    const rows = ledgerRows(CORE, {
      ...PROBED,
      readiness: [...PROBED.readiness, item("config_branch", "unknown", "not checked")],
    });
    expect(rows.find((row) => row.key === "process_config")?.state).toBe("ok");
  });
});

const detail = {
  id: 9042,
  name: "Harbor invoices",
  github_repo: "northwind/harbor-invoices",
  active: false,
  readiness: CORE,
  blockers: [],
  warnings: [],
  processes: [],
  secret_names: [],
  declared_secrets: [],
  runtime_provided_secrets: [],
  settings: {},
} as unknown as ProjectDetail;

describe("sectionRows", () => {
  it("is one page per row the server sent or promised, plus details and run time, in reading order", () => {
    const slugs = sectionRows(detail, PROBED).map(({ section }) => section.slug);
    expect(slugs).toEqual([
      "details",
      "repository-setup",
      "process",
      "ticket-source",
      "runtime",
      "secrets",
    ]);
  });

  it("draws every row about the repository host on Git Source, speaking with its worst", () => {
    const sections = sectionRows(detail, {
      readiness: [
        item("app_access", "ok", "installed on acme"),
        item("labels", "warn", "4 missing"),
        item("branch_protection", "warn", "main is not protected"),
        item("ci_workflows", "ok", "2 workflows"),
        item("gate_approvers", "ok", "no conflict"),
      ],
      warnings: [],
    });
    const setup = sections.find(({ section }) => section.slug === "repository-setup");
    expect(setup?.rows.map((row) => row.key)).toEqual([
      "app_access",
      "branch_protection",
      "ci_workflows",
      "gate_approvers",
    ]);
    expect(setup?.row?.key).toBe("branch_protection");
    // The labels live with the tracker.
    const tickets = sections.find(({ section }) => section.slug === "ticket-source");
    expect(tickets?.rows.map((row) => row.key)).toEqual(["ticket_source", "labels"]);
  });

  it("keeps Details and Git Source pages before any row is promised", () => {
    const slugs = sectionRows({ ...detail, probe_keys: [] }, { readiness: [], warnings: [] }).map(
      ({ section }) => section.slug,
    );
    expect(slugs.slice(0, 2)).toEqual(["details", "repository-setup"]);
  });
});

describe("retiredSection", () => {
  it("sends the addresses of pages that joined another to that page, and nothing else", () => {
    for (const old of ["app-access", "branch-protection", "ci-workflows", "gate-approvers"]) {
      expect(retiredSection(old)).toBe("repository-setup");
    }
    expect(retiredSection("labels")).toBe("ticket-source");
    expect(retiredSection("permits")).toBe("runtime");
    expect(retiredSection("ticket-source")).toBeNull();
    expect(retiredSection("details")).toBeNull();
    expect(retiredSection("constructor")).toBeNull();
    expect(retiredSection(undefined)).toBeNull();
  });
});

describe("ConfigurationLedger", () => {
  function render(probes: ProjectReadiness | undefined, checking = false): string {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return renderToString(
      <MemoryRouter>
        <QueryClientProvider client={qc}>
          <ConfigurationLedger project={detail} probes={probes} checking={checking} />
        </QueryClientProvider>
      </MemoryRouter>,
    );
  }

  it("names the repo-side rows before anyone asked for them, and says so", () => {
    const html = render(undefined);

    expect(html).toContain("Details");
    expect(html).toContain(UNCHECKED);
    expect(html).toContain("Checked when you open it");
    expect(html).not.toContain(CHECKING);
  });

  it("says the probes are on their way while they run", () => {
    const html = render(undefined, true);
    expect(html).toContain(CHECKING);
    expect(html).not.toContain(UNCHECKED);
  });

  it("draws no Config branch row: the process config page owns the branch", () => {
    const html = render({
      ...PROBED,
      readiness: [...PROBED.readiness, item("config_branch", "warn", "missing")],
    });
    expect(html).not.toContain("Config branch");
  });

  it("shows what the probes found once they answer, each row a way to its page", () => {
    const html = render(PROBED);

    expect(html).toContain("4 missing");
    expect(html).not.toContain(UNCHECKED);
    expect(html).toContain('href="/projects/9042/configuration/repository-setup"');
    expect(html).toContain('href="/projects/9042/configuration/details"');
  });
});
