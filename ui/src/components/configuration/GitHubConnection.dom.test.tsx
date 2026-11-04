// @vitest-environment happy-dom
/**
 * The ticket source's GitHub connection: App or access token, which fields each mode shows, what
 * a save sends, and what a reader without `projects:manage` is left with.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../../api/client";
import { keys } from "../../api/keys";
import { holding, MEMBER_PERMISSIONS } from "../../api/organization.fixture";
import type { GitHubConnection, ProjectDetail } from "../../api/types";
import { choose, click, type Mounted, mount, settle, type } from "../../testDom";
import { TicketSourceBody } from "./ProjectConfig";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.restoreAllMocks();
});

const detail = {
  id: 9042,
  name: "widgets",
  github_repo: "northwind/widgets",
  settings: { github: { mode: "app", app_id: "123" } },
  ticket_source: {
    id: 1,
    project_id: 9042,
    kind: "github",
    external_ref: "northwind/widgets",
    webhook_secret_ref: null,
    last_polled_at: null,
    created_at: "2026-08-01T00:00:00Z",
  },
  secret_names: [],
  declared_secrets: [],
  runtime_provided_secrets: [],
  secret_deliveries: [],
  blockers: [],
  warnings: [],
  readiness: [],
} as unknown as ProjectDetail;

const APP: GitHubConnection = {
  mode: "app",
  app_id: "123",
  bot_login: "northwind-bot[bot]",
  installations: { northwind: 7 },
  private_key_set: true,
  webhook_secret_set: true,
  token_set: false,
  webhook_url: "https://factorial.example.com/factorial/webhooks/github/apps/123",
};

async function mounted(
  connection: GitHubConnection = APP,
  permissions?: string[],
): Promise<HTMLElement> {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  holding(qc, permissions);
  qc.setQueryData(keys.githubConnection(detail.id), connection);
  vi.spyOn(api, "getGitHubConnection").mockResolvedValue(connection);
  view = await mount(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <TicketSourceBody project={detail} rows={[]} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return view.container;
}

const field = (root: HTMLElement, label: string): HTMLInputElement | null => {
  for (const each of root.querySelectorAll("label")) {
    if (each.textContent?.trim().startsWith(label)) {
      const id = each.getAttribute("for");
      return (id && root.querySelector<HTMLInputElement>(`#${CSS.escape(id)}`)) || null;
    }
  }
  return null;
};

describe("the GitHub connection", () => {
  it("shows App fields by default and Token fields when chosen", async () => {
    const root = await mounted({ ...APP, mode: null, app_id: "", webhook_url: "" });
    expect(root.textContent).toContain("Connect with");
    expect(field(root, "App ID")).not.toBeNull();
    expect(field(root, "Private key")).not.toBeNull();
    expect(field(root, "Access token")).toBeNull();
    await choose(field(root, "Connect with"), "token");
    expect(field(root, "Access token")).not.toBeNull();
    expect(field(root, "App ID")).toBeNull();
    expect(field(root, "Private key")).toBeNull();
    expect(field(root, "Webhook secret")).not.toBeNull();
    expect(field(root, "Bot username")).not.toBeNull();
  });

  it("shows the token consequences next to Access token", async () => {
    const root = await mounted({ ...APP, mode: "token", token_set: true });
    expect(root.textContent).toContain(
      "Every agent run of this project gets this token, and it does not expire on its own. It may reach more than this repository. Factorial cannot check that it lacks the workflows permission. Whether its account can approve its own pull requests depends on that account's rights.",
    );
    expect(root.textContent).toContain(
      "A fine-grained token for these repositories with: Contents, Issues and Pull requests read and write; Checks and Metadata read.",
    );
    expect(root.textContent).toContain("Leave blank to keep the stored token.");
  });

  it("sends only the fields typed, never empty secrets", async () => {
    const root = await mounted();
    const put = vi.spyOn(api, "putGitHubConnection").mockResolvedValue(APP);
    vi.spyOn(api, "putTicketSource").mockResolvedValue({} as never);
    const update = vi.spyOn(api, "updateProject").mockResolvedValue({} as never);
    await type(field(root, "Private key"), "-----BEGIN KEY-----");
    await click(root.querySelector("button[type='submit']"));
    await settle();
    expect(put).toHaveBeenCalledWith(9042, {
      mode: "app",
      app_id: "123",
      private_key: "-----BEGIN KEY-----",
    });
    expect(update).not.toHaveBeenCalled();
  });

  it("sends the connection only once the ticket source is saved", async () => {
    const root = await mounted();
    const put = vi.spyOn(api, "putGitHubConnection").mockResolvedValue(APP);
    let saved: (value: never) => void = () => {};
    vi.spyOn(api, "putTicketSource").mockReturnValue(
      new Promise((resolve) => {
        saved = resolve;
      }),
    );
    await type(field(root, "Private key"), "-----BEGIN KEY-----");
    await click(root.querySelector("button[type='submit']"));
    await settle();
    expect(api.putTicketSource).toHaveBeenCalled();
    expect(put).not.toHaveBeenCalled();
    saved({} as never);
    await settle();
    expect(put).toHaveBeenCalledTimes(1);
  });

  it("sends no connection when the ticket source is refused", async () => {
    const root = await mounted();
    const put = vi.spyOn(api, "putGitHubConnection").mockResolvedValue(APP);
    vi.spyOn(api, "putTicketSource").mockRejectedValue(new Error("refused"));
    await type(field(root, "Private key"), "-----BEGIN KEY-----");
    await click(root.querySelector("button[type='submit']"));
    await settle();
    expect(put).not.toHaveBeenCalled();
  });

  it("sends no bot username after switching from App to token", async () => {
    const root = await mounted();
    const put = vi.spyOn(api, "putGitHubConnection").mockResolvedValue(APP);
    vi.spyOn(api, "putTicketSource").mockResolvedValue({} as never);
    await choose(field(root, "Connect with"), "token");
    await type(field(root, "Access token"), "ghp_new");
    await click(root.querySelector("button[type='submit']"));
    await settle();
    expect(put).toHaveBeenCalledWith(9042, { mode: "token", token: "ghp_new" });
  });

  it("sends a token project's bot username only when it was edited", async () => {
    const root = await mounted({ ...APP, mode: "token", token_set: true });
    const put = vi.spyOn(api, "putGitHubConnection").mockResolvedValue(APP);
    vi.spyOn(api, "putTicketSource").mockResolvedValue({} as never);
    await type(field(root, "Bot username"), " ci-bot ");
    await click(root.querySelector("button[type='submit']"));
    await settle();
    expect(put).toHaveBeenCalledWith(9042, { mode: "token", bot_login: "ci-bot" });
  });

  it("shows the webhook URL from the connection", async () => {
    const root = await mounted();
    expect(root.textContent).toContain("Webhook URL");
    const shown = field(root, "Webhook URL");
    expect(shown?.value).toBe(APP.webhook_url);
    expect(shown?.readOnly).toBe(true);
    expect(root.textContent).toContain("Copy");
  });

  it("a reader sees the connection read-only with no secret inputs", async () => {
    const root = await mounted(APP, MEMBER_PERMISSIONS);
    expect(field(root, "App ID")?.readOnly).toBe(true);
    expect(field(root, "Private key")).toBeNull();
    expect(field(root, "Webhook secret")).toBeNull();
    expect(root.querySelector("button[type='submit']")).toBeNull();
    expect(field(root, "Webhook URL")?.value).toBe(APP.webhook_url);
  });

  it("a reader sees which secrets are stored and where the App is installed, with no inputs", async () => {
    const root = await mounted(
      { ...APP, private_key_set: true, webhook_secret_set: false },
      MEMBER_PERMISSIONS,
    );
    const states = [...root.querySelectorAll("[data-stored-state]")].map(
      (each) => each.textContent,
    );
    expect(states).toEqual(["Private key: Stored", "Webhook secret: Not set"]);
    expect(root.textContent).toContain("Installed on");
    expect(root.textContent).toContain("northwind · installation 7");
    expect(root.querySelector("textarea")).toBeNull();
    expect(root.querySelector("input[type='password']")).toBeNull();
  });

  it("a manager sees Stored beside a stored key's input", async () => {
    const root = await mounted({ ...APP, mode: "token", token_set: true });
    const input = field(root, "Access token");
    expect(input?.parentElement?.parentElement?.textContent).toContain("Access token: Stored");
    expect(root.textContent).not.toContain("Installed on");
  });
});
