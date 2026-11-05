// @vitest-environment happy-dom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { forgeHost } from "../../forge";
import { byText, click, mount, settle } from "../../testDom";
import { AdoptHeadDialog } from "./AdoptHeadDialog";

const HEAD = "e".repeat(40);
const NEWER = "f".repeat(40);

function preview(head: string) {
  return {
    head_sha: head,
    known_base: "a".repeat(40),
    commits: [
      { sha: head, message: "Tighten the spec", author_name: "operator", authored_at: null },
    ],
    files: [
      {
        path: ".factorial/processes/process-p.yaml",
        status: "modified",
        additions: 3,
        deletions: 1,
        previous_path: null,
      },
    ],
    total_commits: 1,
    files_truncated: false,
    compare_url: "https://github.com/o/r/compare/a...e",
  };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("AdoptHeadDialog", () => {
  it("enables Adopt and sync only once the person ticks that they read the changes", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json(preview(HEAD))),
    );
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = await mount(
      <QueryClientProvider client={qc}>
        <AdoptHeadDialog projectId={3} onClose={() => {}} onAdopted={() => {}} />
      </QueryClientProvider>,
    );
    await settle();
    const confirm = byText(document.body, "button", "Adopt and sync");
    expect(confirm?.getAttribute("aria-disabled")).toBe("true");
    await click(byText(document.body, "label", "I've read these changes and confirm them"));
    expect(confirm?.getAttribute("aria-disabled")).not.toBe("true");
    await view.unmount();
  });

  it("hands the result over without reading the preview again, which now answers 409", async () => {
    let adopted = false;
    let previews = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.endsWith("/adoptions")) {
          adopted = true;
          return json({ sha: HEAD, adopted: true, sync: null, sync_error: null }, 201);
        }
        if (url.endsWith("/unknown-head")) {
          previews += 1;
          if (adopted) return json({ detail: "already known: nothing to adopt" }, 409);
          return json(preview(HEAD));
        }
        return json({});
      }),
    );
    const onAdopted = vi.fn(() => previews);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = await mount(
      <QueryClientProvider client={qc}>
        <AdoptHeadDialog projectId={3} onClose={() => {}} onAdopted={onAdopted} />
      </QueryClientProvider>,
    );
    await settle();
    await click(byText(document.body, "label", "I've read these changes and confirm them"));
    await click(byText(document.body, "button", "Adopt and sync"));
    await settle();
    expect(onAdopted).toHaveBeenCalledOnce();
    expect(onAdopted.mock.results[0]?.value).toBe(1);
    await view.unmount();
  });

  it("shows the new head and clears the tick when the branch moved before the click", async () => {
    let head = HEAD;
    const posted: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (url.endsWith("/adoptions")) {
          posted.push(JSON.parse(String(init?.body)).sha);
          head = NEWER;
          return json({ detail: "the `factorial` branch moved to fffffff" }, 409);
        }
        return json(preview(head));
      }),
    );
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = await mount(
      <QueryClientProvider client={qc}>
        <AdoptHeadDialog projectId={3} onClose={() => {}} onAdopted={() => {}} />
      </QueryClientProvider>,
    );
    await settle();
    await click(byText(document.body, "label", "I've read these changes and confirm them"));
    await click(byText(document.body, "button", "Adopt and sync"));
    await settle();
    await settle();
    expect(posted).toEqual([HEAD]);
    expect(document.body.textContent).toContain("fffffff");
    expect(byText(document.body, "button", "Adopt and sync")?.getAttribute("aria-disabled")).toBe(
      "true",
    );
    await view.unmount();
  });

  it("names a GitLab project's host on the comparison and its links", async () => {
    const compared = {
      ...preview(HEAD),
      total_commits: 3,
      files_truncated: true,
      compare_url: "https://gitlab.example.com/o/r/-/compare/a...e",
    };
    let body: unknown = compared;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json(body)),
    );
    const forge = forgeHost({ kind: "gitlab" }, {});
    const dialog = () => (
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <AdoptHeadDialog projectId={3} forge={forge} onClose={() => {}} onAdopted={() => {}} />
      </QueryClientProvider>
    );
    let view = await mount(dialog());
    await settle();
    let text = document.body.textContent ?? "";
    expect(text).toContain("2 more in the comparison on GitLab.");
    expect(text).toContain("This list stops at 300 files. The comparison shows the rest.");
    expect(byText(document.body, "a", "Compare aaaaaaa with eeeeeee on GitLab")).not.toBeNull();
    expect(text).not.toContain("GitHub");
    await view.unmount();

    body = { ...compared, known_base: null };
    view = await mount(dialog());
    await settle();
    text = document.body.textContent ?? "";
    expect(byText(document.body, "a", "Open eeeeeee on GitLab")).not.toBeNull();
    expect(text).not.toContain("GitHub");
    await view.unmount();
  });
});
