/**
 * The Runtimes pages' structural invariants: what the list says about each runtime, which
 * controls the detail page offers per type, and what the create page never puts in its markup.
 *
 * Rendered with `renderToString` against a pre-seeded query cache, as `TicketPage.test.tsx` is:
 * no fetch, no DOM, and the data is there synchronously so the page draws its real content.
 * Effects do not run, which is what makes the FIRST paint the thing under test.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { keys } from "../api/queries";
import type { Runtime, RuntimeProject } from "../api/types";
import { RUNTIME_TYPES } from "../components/runtimes/runtimes";
import { formatRelative } from "../format";
import { NewRuntimePage } from "./NewRuntimePage";
import { RuntimePage } from "./RuntimePage";
import { RuntimesPage } from "./RuntimesPage";

/**
 * The control a visible label names, found through the label's `for` — the link a screen reader
 * follows. Ids are generated (`useId`), so a test that pinned one would pin an implementation. A
 * required field's label carries its marker after the words.
 */
function control(html: string, label: string): string | null {
  const id = html.match(
    new RegExp(`<label[^>]*for="([^"]+)"[^>]*>${label}(?:<span[^>]*>.*?</span>)?</label>`),
  )?.[1];
  if (!id) return null;
  const escaped = id.replace(/[^\w-]/g, "\\$&");
  return (
    html.match(new RegExp(`<(?:input|select|textarea)[^>]*id="${escaped}"[^>]*>`))?.[0] ?? null
  );
}

const STORED_AT = "2026-09-01T09:00:00Z";
const REPLACED_AT = "2026-09-05T15:30:00Z";

function runtime(
  id: number,
  slug: string,
  type: Runtime["type"],
  names: string[] = [],
  metadata: Record<string, string> = {},
  connected_projects = 0,
): Runtime {
  return {
    id,
    slug,
    name: slug.replace(/-/g, " "),
    type,
    details: {},
    metadata,
    credential_names: names,
    credentials: names.map((name) => ({ name, created_at: STORED_AT, updated_at: STORED_AT })),
    connected_projects,
    last_auth_failure: null,
    created_at: STORED_AT,
    updated_at: STORED_AT,
  };
}

const RUNTIMES: Runtime[] = [
  runtime(1, "codex-key", "codex_api"),
  runtime(2, "codex-sub", "codex_subscription"),
  runtime(3, "codex-team", "codex_subscription", ["CODEX_AUTH_JSON"], {}, 2),
  runtime(4, "claude-sub", "claude_subscription"),
  runtime(5, "codex-tagged", "codex_api", [], { owner: "platform", "cost.center": "cc-42" }),
  runtime(6, "codex-stray", "codex_api", ["OTHER"]),
  runtime(7, "claude-key", "claude_api", ["ANTHROPIC_API_KEY"], {}, 1),
  runtime(8, "codex-keyed", "codex_api", ["OPENAI_API_KEY"]),
  runtime(9, "claude-team", "claude_subscription", ["CLAUDE_CODE_OAUTH_TOKEN"]),
];

const PROJECTS: Record<number, RuntimeProject[]> = {
  3: [
    { id: 11, name: "widgets", github_repo: "acme/widgets" },
    { id: 12, name: "gadgets", github_repo: "acme/gadgets" },
  ],
  7: [{ id: 13, name: "sprockets", github_repo: "acme/sprockets" }],
};

const OFFERED_EVERYWHERE = RUNTIME_TYPES.flatMap((type) => [`${type}@local_cli`, `${type}@e2b`]);

function render(url: string, rows: Runtime[] = RUNTIMES): string {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(keys.runtimes, rows);
  qc.setQueryData(keys.runtimeOptions, { offered: OFFERED_EVERYWHERE });
  for (const row of rows) qc.setQueryData(keys.runtimeProjects(row.id), PROJECTS[row.id] ?? []);
  return renderToString(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route path="/runtimes" element={<RuntimesPage />} />
          <Route path="/runtimes/new" element={<NewRuntimePage />} />
          <Route path="/runtimes/:slug" element={<RuntimePage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const count = (html: string, needle: string) => html.split(needle).length - 1;

/** The one-line key or token field, by the id the page gives it for its label. */
const CREDENTIAL_FIELD = /<input(?=[^>]*id="field-credential")[^>]*>/;

describe("the list", () => {
  it("links each runtime to its own page, with its slug, its type in words, and whether it can sign in", () => {
    const html = render("/runtimes");
    for (const slug of ["codex-key", "codex-sub", "codex-team", "claude-sub"]) {
      expect(html).toContain(`href="/runtimes/${slug}"`);
    }
    expect(html).toContain("Codex · API key");
    expect(html).toContain("Codex · Subscription");
    // Connected is exact-name for every type: the bundle and the two keys count, the stray
    // name under an api-key runtime does not, and the empty rows hold nothing.
    expect(count(html, ">Connected<")).toBe(4);
    expect(count(html, ">None<")).toBe(5);
  });

  it("heads its columns Name, Slug, CLI and authentication, Sandbox, Connected repositories, Credential, Created", () => {
    const columns = [...render("/runtimes").matchAll(/<th[^>]*>([^<]*)<\/th>/g)].map((m) => m[1]);
    expect(columns).toEqual([
      "Name",
      "Slug",
      "CLI and authentication",
      "Sandbox",
      "Connected repositories",
      "Credential",
      "Created",
    ]);
  });

  it("counts the repositories whose process names each runtime", () => {
    const rows = RUNTIMES.filter((row) =>
      ["codex-team", "claude-key", "codex-key"].includes(row.slug),
    );
    const html = render("/runtimes", rows);
    const cells = [...html.matchAll(/<td[^>]*>(\d+)<\/td>/g)].map((m) => m[1]);
    expect(cells).toEqual(["0", "2", "1"]);
  });

  it("says so when there is nothing yet, and still offers to create one", () => {
    const html = render("/runtimes", []);
    expect(html).toContain("No runtimes yet");
    expect(html).toContain('href="/runtimes/new"');
  });

  it("holds no form of its own", () => {
    expect(control(render("/runtimes"), "Slug")).toBeNull();
  });
});

describe("the create page", () => {
  it("asks CLI, Authentication and Sandbox on cards", () => {
    const html = render("/runtimes/new");
    for (const card of [
      "Claude Code",
      "Codex",
      "API key",
      "Subscription",
      "On the host",
      "Managed cloud sandbox",
    ]) {
      expect(html).toContain(card);
    }
    for (const unbuilt of [
      "AWS Bedrock (Commercial)",
      "AWS Bedrock (GovCloud)",
      "Self-hosted sandbox",
    ]) {
      expect(html).not.toContain(unbuilt);
    }
  });

  it("renders no secret field with a value", () => {
    // A key in the markup would sit in the DOM, the page source and every snapshot of it.
    const html = render("/runtimes/new");
    expect(html).toMatch(/<input[^>]*type="password"/);
    expect(html).not.toMatch(/<input(?=[^>]*type="password")[^>]*value=/);
  });
});

describe("the runtime page", () => {
  it("lets the name change in place and holds slug and type as text, not fields", () => {
    const html = render("/runtimes/codex-sub");
    // Read until pressed: the value, and a button that says what pressing it edits.
    expect(html).toContain('aria-label="Edit name: codex sub"');
    expect(control(html, "Slug")).toBeNull();
    expect(html).not.toContain("<select");
    // Deleting sits behind the header's More menu, which asks before it acts.
    expect(html).toContain('aria-label="More actions"');
  });

  it("shows the runtime's isolation as text: a row from before the field runs on the host", () => {
    const html = render("/runtimes/codex-sub");
    expect(html).toContain("Sandbox");
    expect(html).toContain("On the host");
    expect(html).not.toContain("<select");
  });

  it("gives a subscription runtime the device login first, and the pasted bundle behind a button", () => {
    const html = render("/runtimes/codex-sub");
    expect(html).toContain("Connect subscription");
    expect(html).toContain("Paste auth.json");
    expect(html).toContain("Needed to run");
    expect(html).not.toContain("<textarea");
    expect(html).not.toContain('id="field-credential"');
  });

  it("shows a stored credential as its name and its age, and offers to remove it", () => {
    const html = render("/runtimes/codex-team");
    expect(html).toContain("CODEX_AUTH_JSON");
    expect(html).toContain(`stored ${formatRelative(STORED_AT)}`);
    expect(html).not.toContain("updated ");
    expect(html).toContain("Stored");
    expect(html).toContain("Remove");
  });

  it("dates a replaced credential by both clocks", () => {
    const row = runtime(8, "claude-rotated", "claude_api", ["ANTHROPIC_API_KEY"]);
    row.credentials = row.credentials.map((c) => ({ ...c, updated_at: REPLACED_AT }));
    const html = render("/runtimes/claude-rotated", [row]);
    expect(html).toContain(`stored ${formatRelative(STORED_AT)}`);
    expect(html).toContain(`updated ${formatRelative(REPLACED_AT)}`);
  });

  it("offers a stored bundle a reconnect, still with no value shown", () => {
    const html = render("/runtimes/codex-team");
    expect(html).toContain("Reconnect subscription");
    expect(html).not.toContain("<textarea");
  });

  it("opens a missing key's password field, which never carries a value — not even an empty one", () => {
    const html = render("/runtimes/codex-key");
    expect(html).toContain("OpenAI API key");
    expect(html).toContain("Needed to run");
    expect(html).toContain("Save key");
    expect(html).not.toContain("Replace");
    expect(html).not.toContain("Connect subscription");
    expect(html).not.toContain("<textarea");
    const field = html.match(CREDENTIAL_FIELD)?.[0];
    expect(field).toContain('type="password"');
    expect(field).toMatch(/autocomplete="off"/i);
    expect(field).not.toContain("value=");
  });

  it("keeps a stored key's field closed behind Replace", () => {
    const html = render("/runtimes/codex-keyed");
    expect(html).toContain("OPENAI_API_KEY");
    expect(html).toContain("Replace");
    expect(html).not.toMatch(CREDENTIAL_FIELD);
  });

  it("keys the row on the type: a Claude runtime asks for the Anthropic key", () => {
    const html = render("/runtimes/claude-key");
    expect(html).toContain("Anthropic API key");
    expect(html).toContain("ANTHROPIC_API_KEY");
    expect(html).toContain("Replace");
  });

  it("counts only the type's own name as stored: a stray name is listed apart, removable", () => {
    const html = render("/runtimes/codex-stray");
    expect(html).toContain("Unused credentials");
    expect(html).toContain("OTHER");
    expect(html).toContain("Remove");
    expect(html).toMatch(CREDENTIAL_FIELD);
    expect(html).not.toContain("Replace");
  });

  it("gives an E2B runtime its team's key and template beside the provider's key", () => {
    const row = runtime(10, "claude-e2b", "claude_api", ["ANTHROPIC_API_KEY"]);
    row.details = { isolation: "e2b" };
    const html = render("/runtimes/claude-e2b", [row]);
    expect(html).toContain("Cloud sandbox");
    expect(html).toContain('id="field-credential-e2b"');
    // The template is chosen from the key's team, so nothing offers one before the key.
    expect(html).toContain("once its sandbox API key is stored");
    expect(html).not.toContain('id="field-template"');
    expect(count(html, "Needed to run")).toBe(2);
  });

  it("gives a claude subscription one way in: the pasted setup-token, under its own name", () => {
    const html = render("/runtimes/claude-sub");
    expect(html).not.toContain("not built yet");
    // No device flow exists for Claude Code, so no button that would 409 — and the paste form
    // stores under the type's credential name, never the codex bundle's.
    expect(html).toContain("setup-token");
    expect(html).toContain("CLAUDE_CODE_OAUTH_TOKEN");
    expect(html).toContain("Save token");
    expect(html.match(CREDENTIAL_FIELD)?.[0]).toContain('type="password"');
    expect(html).not.toContain("Connect subscription");
    expect(html).not.toContain("auth.json");
    expect(html).not.toContain("CODEX_AUTH_JSON");
  });

  it("names the stored token on a connected claude subscription", () => {
    const html = render("/runtimes/claude-team");
    expect(html).toContain("CLAUDE_CODE_OAUTH_TOKEN");
    expect(html).toContain("Remove");
  });

  it("lists each metadata pair as an editable row, and offers to add one", () => {
    const html = render("/runtimes/codex-tagged");
    expect(html).toContain("Metadata");
    expect(html).toContain('value="owner"');
    expect(html).toContain('value="platform"');
    expect(html).toContain('value="cost.center"');
    expect(html).toContain('value="cc-42"');
    expect(html).toContain("Add row");
    expect(count(html, 'aria-label="Remove row')).toBe(2);
  });

  it("offers the add-row control on a runtime with no metadata, and no empty row before it", () => {
    const html = render("/runtimes/codex-key");
    expect(html).toContain("Metadata");
    expect(html).toContain("Add row");
    expect(html).not.toContain('name="metadata-key"');
  });

  it("carries no hint under Metadata", () => {
    expect(render("/runtimes/codex-key")).not.toContain("Notes on this runtime for operators");
  });

  it("ends with the connected repositories: each project linked, each repository on GitHub", () => {
    const html = render("/runtimes/codex-team");
    const heading = html.indexOf("Connected repositories");
    expect(heading).toBeGreaterThan(html.indexOf("Metadata"));
    expect(heading).toBeGreaterThan(html.indexOf("Credentials"));
    expect(html).toContain('href="/projects/11"');
    expect(html).toContain("widgets");
    expect(html).toContain('href="/projects/12"');
    expect(html).toContain("gadgets");
    const repo = html.match(/<a[^>]*href="https:\/\/github.com\/acme\/widgets"[^>]*>/)?.[0];
    expect(repo).toContain('target="_blank"');
    expect(repo).toMatch(/rel="[^"]*noopener/);
    expect(html).not.toContain("No process names this runtime yet");
  });

  it("says when no process names the runtime yet", () => {
    const html = render("/runtimes/codex-key");
    expect(html).toContain("Connected repositories");
    expect(html).toContain("No process names this runtime yet");
    expect(html).not.toContain('href="/projects/');
  });

  it("says when the slug in the URL is not a runtime, rather than showing nothing", () => {
    const html = render("/runtimes/gone");
    expect(html).toContain("Runtime not found");
    expect(html).toContain("No runtime has the slug");
    expect(html).toContain('href="/runtimes"');
  });
});
