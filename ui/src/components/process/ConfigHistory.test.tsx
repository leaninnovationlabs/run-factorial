// @vitest-environment happy-dom
/**
 * The config history drawer: every synced commit, and the way back to one. A restore is a commit,
 * so it is confirmed with what it will write, delete and retire, read from the server first.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { keys } from "../../api/keys";
import type { ProcessVersion, ProjectDetail } from "../../api/types";
import { click, type Mounted, mount, settle } from "../../testDom";
import { clearFlags, FlagGroup } from "../ui/Flag";
import { ConfigHistory } from "./ConfigHistory";

const PROJECT = {
  id: 7,
  name: "harbor",
  github_repo: "northwind/harbor-invoices",
} as ProjectDetail;
const OLD = "a".repeat(40);
const NEW = "b".repeat(40);
const HEAD = "c".repeat(40);

const row = (id: number, sha: string, activated_at: string): ProcessVersion => ({
  id,
  project_id: PROJECT.id,
  process_key: "standard",
  source_repo: PROJECT.github_repo,
  source_commit_sha: sha,
  retired_at: null,
  source_ref: "factorial",
  adopted_by: null,
  activated_at,
  created_at: activated_at,
});
const VERSIONS = [row(2, NEW, "2026-11-05T10:00:00Z"), row(1, OLD, "2026-11-01T10:00:00Z")];

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

let view: Mounted | null = null;
afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.unstubAllGlobals();
  clearFlags();
});

/** The drawer open on two synced commits, with `preview` answering the restore preview. */
async function opened(preview: unknown, onClose = vi.fn()) {
  const restores: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/process-versions/1/restore")) return json(preview);
      if (url.endsWith("/config-restores")) {
        restores.push(JSON.parse(String(init?.body)));
        return json(
          {
            sha: "e".repeat(40),
            url: "https://github.com/northwind/harbor-invoices/commit/eee",
            branch: "factorial",
            committed: true,
            components: [],
            written: [],
            deleted: [],
            sync: null,
            sync_error: null,
          },
          201,
        );
      }
      return json({ detail: "not part of this test" }, 404);
    }),
  );
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(keys.processVersions(PROJECT.id), VERSIONS);
  view = await mount(
    <QueryClientProvider client={qc}>
      <ConfigHistory project={PROJECT} onClose={onClose} />
      <FlagGroup />
    </QueryClientProvider>,
  );
  await settle();
  return { restores, onClose };
}

/** The row of the older commit: its Restore is the last in the list. */
const restoreOlder = () =>
  [...document.querySelectorAll("li button")].filter((b) => b.textContent === "Restore").at(-1);
const PREVIEW = {
  target_sha: OLD,
  head_sha: HEAD,
  writes: [".factorial/a.md", ".factorial/b.md"],
  deletes: [".factorial/c.md"],
  retires: ["fast"],
  unchanged: false,
};

describe("ConfigHistory", () => {
  it("lists every synced commit, and offers Restore on each but the current one", async () => {
    await opened(PREVIEW);
    const text = document.body.textContent ?? "";
    expect(text).toContain("Config history");
    expect(text).toContain(NEW.slice(0, 7));
    expect(text).toContain(OLD.slice(0, 7));
    expect(text).toContain("Current");
    // The current commit is what runs already: there is nothing to go back to.
    const rows = [...document.querySelectorAll("li")];
    expect(rows).toHaveLength(2);
    expect(rows[0]?.textContent).toContain("Current");
    expect(rows[0]?.querySelector("button")).toBeNull();
    expect(rows[1]?.querySelector("button")?.textContent).toBe("Restore");
  });

  it("confirms with what the restore rewrites, deletes and retires", async () => {
    const { restores } = await opened(PREVIEW);
    await click(restoreOlder());
    await settle();
    const text = document.body.textContent ?? "";
    expect(text).toContain(`Restore the config to ${OLD.slice(0, 7)}?`);
    expect(text).toContain("Rewrites 2 files and deletes 1 under .factorial/");
    expect(text).toContain("Retires fast.");
    expect(text).toContain("Tickets already running keep the version they were admitted on.");
    expect(restores).toEqual([]);
  });

  it("restores against the head the preview read, says so, and closes", async () => {
    const { restores, onClose } = await opened(PREVIEW);
    await click(restoreOlder());
    await settle();
    const confirm = [
      ...document.querySelectorAll('[role="dialog"] button, [role="alertdialog"] button'),
    ]
      .filter((button) => button.textContent === "Restore")
      .at(-1);
    await click(confirm);
    await settle();
    expect(restores).toEqual([{ version_id: 1, expected_head: HEAD }]);
    expect(document.body.textContent).toContain(`Restored the config to ${OLD.slice(0, 7)}`);
    expect(onClose).toHaveBeenCalled();
  });

  it("says the branch already holds a version whose preview is unchanged", async () => {
    const { restores } = await opened({
      ...PREVIEW,
      writes: [],
      deletes: [],
      retires: [],
      unchanged: true,
    });
    await click(restoreOlder());
    await settle();
    expect(document.body.textContent).toContain("The branch already holds this version");
    expect(document.body.textContent).not.toContain("Restore the config to");
    expect(restores).toEqual([]);
  });
});
