/**
 * The client's error handling, which is where a mistake is invisible: a wrong status mapping
 * shows the user "internal error" instead of "sync .factorial/process.yaml first", and a missed 401
 * loops the app on a token it will never accept.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, SERVER_FAILED, setOrganization, UNREACHABLE } from "./client";

/** A real Response, so an empty body reads the way a browser reads it. */
function respond(status: number, body: unknown): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), { status });
}

const fetchMock = vi.fn();

/** The one fetch the call under test made. Asserting rather than indexing keeps
 *  `noUncheckedIndexedAccess` honest — a test that made no request must fail loudly. */
function lastCall(): { url: string; init: RequestInit & { headers: Record<string, string> } } {
  const call = fetchMock.mock.calls.at(-1);
  if (!call) throw new Error("no fetch was made");
  return { url: call[0], init: call[1] };
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("credentials", () => {
  it("sends the session cookie and never an Authorization header", async () => {
    // Nobody signs in to the dashboard with the API token (design.md §7.19).
    fetchMock.mockResolvedValue(respond(200, []));
    await api.listProjects();
    expect(lastCall().init.credentials).toBe("include");
    expect(lastCall().init.headers.Authorization).toBeUndefined();
  });
});

describe("errors", () => {
  it("marks a 401 as unauthorized so the app can re-prompt", async () => {
    fetchMock.mockResolvedValue(respond(401, { detail: "missing or invalid bearer token" }));
    const error = await api.listProjects().catch((caught) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.unauthorized).toBe(true);
  });

  it("surfaces FastAPI's `detail`, which is the operator-facing reason", async () => {
    fetchMock.mockResolvedValue(
      respond(409, { detail: "project is not ready to activate: no ticket source" }),
    );
    const error = await api.setProjectActive(1, true).catch((caught) => caught);
    expect(error.status).toBe(409);
    expect(error.message).toContain("no ticket source");
    expect(error.unauthorized).toBe(false);
  });

  it("flattens FastAPI's per-field 422 list into something readable", async () => {
    fetchMock.mockResolvedValue(
      respond(422, {
        detail: [
          { loc: ["body", "github_repo"], msg: "String should match pattern" },
          { loc: ["body", "name"], msg: "Name must not be blank." },
        ],
      }),
    );
    const error = await api
      .createProject({ name: "x", github_repo: "bad" })
      .catch((caught) => caught);
    expect(error.message).toBe(
      "github_repo: String should match pattern. name: Name must not be blank.",
    );
  });

  it("tells the reader an unreachable API plainly, and keeps the CORS checklist for an operator", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    const error = await api.listProjects().catch((caught) => caught);
    expect(error.status).toBe(0);
    expect(error.message).toBe(UNREACHABLE);
    expect(error.detail).toContain("FACTORIAL_CORS_ORIGINS");
    expect(error.detail).toContain("Failed to fetch");
  });

  it("says a 5xx with no reason failed on our side, and keeps the status line as the detail", async () => {
    fetchMock.mockResolvedValue(respond(500, "Internal Server Error"));
    const error = await api.listProjects().catch((caught) => caught);
    expect(error.status).toBe(500);
    expect(error.message).toBe(SERVER_FAILED);
    expect(error.detail).toContain("500");
  });

  it("says the API's own catch-all for a crash failed on our side", async () => {
    fetchMock.mockResolvedValue(respond(500, { detail: "internal server error" }));
    const error = await api.listProjects().catch((caught) => caught);
    expect(error.message).toBe(SERVER_FAILED);
    expect(error.detail).toBe("internal server error");
  });

  it("keeps a 5xx refusal the API worded as the message, since it names the next step", async () => {
    fetchMock.mockResolvedValue(respond(503, { detail: "this project has no GitHub connection" }));
    const error = await api.listProjects().catch((caught) => caught);
    expect(error.status).toBe(503);
    expect(error.message).toBe("this project has no GitHub connection");
  });

  it("keeps a 4xx refusal's own words as the message, with no detail behind them", async () => {
    fetchMock.mockResolvedValue(respond(409, { detail: "the project is paused" }));
    const error = await api.listProjects().catch((caught) => caught);
    expect(error.message).toBe("the project is paused");
    expect(error.detail).toBeNull();
  });

  it("reads the next step a refusal names beside its detail", async () => {
    fetchMock.mockResolvedValue(
      respond(409, {
        detail: "no process yet",
        code: "not_ready",
        hint: "Sync one from the repository.",
      }),
    );
    const error = await api.setProjectActive(1, true).catch((caught) => caught);
    expect(error.message).toBe("no process yet");
    expect(error.code).toBe("not_ready");
    expect(error.hint).toBe("Sync one from the repository.");
  });

  it("has no hint when the refusal names none", async () => {
    fetchMock.mockResolvedValue(respond(404, { detail: "no such project" }));
    const error = await api.listProjects().catch((caught) => caught);
    expect(error.hint).toBeNull();
  });
});

describe("request shapes", () => {
  it("restarts a ticket on the snapshot it names, and asks for nothing more", async () => {
    fetchMock.mockResolvedValue(respond(202, { status: "requested", landed: true }));
    await api.restartTicket(5204, { process_version_id: 18 });
    const { url, init } = lastCall();
    expect(url).toMatch(/\/api\/tickets\/5204\/restart$/);
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ process_version_id: 18 });
  });

  it("exits a ticket with no body, and asks for the issue closed only on Close and exit", async () => {
    fetchMock.mockImplementation(async () => respond(202, { status: "requested", landed: true }));
    await api.exitTicket(5204);
    expect(lastCall().url).toMatch(/\/api\/tickets\/5204\/exit$/);
    expect(lastCall().init.body).toBeUndefined();

    await api.exitTicket(5204, { close_issue: true });
    expect(JSON.parse(String(lastCall().init.body))).toEqual({ close_issue: true });
  });

  it("settles a stop the API accepted with an empty 202, rather than failing to read JSON", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 202 }));
    await expect(api.cancelAssistantTurn(7)).resolves.toBeUndefined();
    expect(lastCall().url).toMatch(/\/api\/assistant\/turns\/7\/cancel$/);
  });

  it("reads what a restart would remove from the ticket's own preview route", async () => {
    fetchMock.mockResolvedValue(respond(200, {}));
    await api.getRestartPreview(5204);
    expect(lastCall().url).toMatch(/\/api\/tickets\/5204\/restart\/preview$/);
  });

  it("omits absent ticket filters rather than sending empty values", async () => {
    fetchMock.mockResolvedValue(respond(200, { tickets: [] }));
    await api.listTickets(7);
    expect(lastCall().url).toMatch(/\/api\/projects\/7\/tickets$/);
  });

  it("sends both ticket filters when set", async () => {
    fetchMock.mockResolvedValue(respond(200, { tickets: [] }));
    await api.listTickets(7, { status: "blocked", openOnly: true });
    expect(lastCall().url).toContain("status=blocked");
    expect(lastCall().url).toContain("open_only=true");
  });

  it("defaults the ticket source kind so callers cannot forget it", async () => {
    fetchMock.mockResolvedValue(respond(200, {}));
    await api.putTicketSource(3, { external_ref: "acme/widgets" });
    expect(JSON.parse(String(lastCall().init.body))).toEqual({
      kind: "github",
      external_ref: "acme/widgets",
    });
  });

  it("sends a null ref for 'the repo's factorial branch'", async () => {
    fetchMock.mockResolvedValue(respond(201, {}));
    await api.syncProcessVersions(3, null);
    expect(JSON.parse(String(lastCall().init.body))).toEqual({ ref: null });
  });

  it("adopts the factorial head by its full sha and nothing else", async () => {
    fetchMock.mockResolvedValue(
      respond(201, { sha: "e".repeat(40), adopted: true, sync: null, sync_error: null }),
    );
    await api.adoptConfigHead(3, "e".repeat(40));
    const { url, init } = lastCall();
    expect(url).toMatch(/\/api\/projects\/3\/config-branch\/adoptions$/);
    expect(JSON.parse(String(init.body))).toEqual({ sha: "e".repeat(40) });
  });

  it("escapes a secret name into the path", async () => {
    fetchMock.mockResolvedValue(respond(200, { name: "A/B" }));
    await api.putSecret(3, "A/B", { value: "value" });
    expect(lastCall().url).toContain("/secrets/A%2FB");
  });

  it("removes a project secret by name, with a DELETE", async () => {
    fetchMock.mockResolvedValue(respond(204, undefined));
    await api.deleteSecret(3, "OPENAI_API_KEY");
    const { url, init } = lastCall();
    expect(url).toMatch(/\/api\/projects\/3\/secrets\/OPENAI_API_KEY$/);
    expect(init.method).toBe("DELETE");
  });
});

describe("the overview", () => {
  it("asks for the organization's overview, without a project id, because it spans them", async () => {
    fetchMock.mockResolvedValue(respond(200, { projects: [], tickets: [], done_days: 7 }));
    await api.getOverview();
    expect(lastCall().url).toMatch(/\/api\/orgs\/default\/overview$/);
  });

  it("asks within the organization the page opened under", async () => {
    fetchMock.mockResolvedValue(respond(200, { projects: [], tickets: [], done_days: 7 }));
    setOrganization("acme");
    try {
      await api.getOverview();
    } finally {
      setOrganization("default");
    }
    expect(lastCall().url).toMatch(/\/api\/orgs\/acme\/overview$/);
  });

  it("passes a done window through when one is given", async () => {
    fetchMock.mockResolvedValue(respond(200, { projects: [], tickets: [], done_days: 30 }));
    await api.getOverview(30);
    expect(lastCall().url).toContain("done_days=30");
  });
});

describe("runtimes", () => {
  it("creates with a POST and the wire's own field names", async () => {
    fetchMock.mockResolvedValue(respond(201, { id: 3 }));
    await api.createRuntime({ slug: "codex-sub", name: "Codex sub", type: "codex_subscription" });
    const { url, init } = lastCall();
    expect(url).toMatch(/\/api\/orgs\/default\/runtimes$/);
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      slug: "codex-sub",
      name: "Codex sub",
      type: "codex_subscription",
    });
  });

  it("patches only what was given, so slug and type are never sent by accident", async () => {
    fetchMock.mockResolvedValue(respond(200, { id: 3 }));
    await api.updateRuntime(3, { name: "Renamed" });
    const { url, init } = lastCall();
    expect(url).toMatch(/\/api\/runtimes\/3$/);
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(String(init.body))).toEqual({ name: "Renamed" });
  });

  it("deletes with DELETE and resolves on the empty 204", async () => {
    fetchMock.mockResolvedValue(respond(204, undefined));
    await expect(api.deleteRuntime(3)).resolves.toBeUndefined();
    expect(lastCall().init.method).toBe("DELETE");
  });

  it("escapes a credential name into the path, and sends only the value", async () => {
    fetchMock.mockResolvedValue(respond(200, { name: "A/B" }));
    await api.putRuntimeCredential(3, "A/B", "s3cret");
    const { url, init } = lastCall();
    expect(url).toContain("/api/runtimes/3/credentials/A%2FB");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(String(init.body))).toEqual({ value: "s3cret" });
  });

  it("removes a credential by name with DELETE", async () => {
    fetchMock.mockResolvedValue(respond(204, undefined));
    await api.deleteRuntimeCredential(3, "CODEX_AUTH_JSON");
    const { url, init } = lastCall();
    expect(url).toMatch(/\/api\/runtimes\/3\/credentials\/CODEX_AUTH_JSON$/);
    expect(init.method).toBe("DELETE");
  });

  it("starts a device login with a bodiless POST and polls it by session", async () => {
    fetchMock.mockResolvedValue(respond(202, { session_id: "s-1" }));
    await api.startRuntimeLogin(3);
    expect(lastCall().url).toMatch(/\/api\/runtimes\/3\/login$/);
    expect(lastCall().init.method).toBe("POST");
    expect(lastCall().init.body).toBeUndefined();

    fetchMock.mockResolvedValue(respond(200, { session_id: "s-1", status: "pending" }));
    await api.getRuntimeLoginStatus(3, "s-1");
    expect(lastCall().url).toMatch(/\/api\/runtimes\/3\/login\/s-1$/);
  });
});

describe("the assistant", () => {
  it("keeps a refusal's closed code beside its detail", async () => {
    fetchMock.mockResolvedValue(
      respond(409, { code: "turn_live", detail: "a turn is already running" }),
    );
    const error = await api
      .sendAssistantMessage(7, { text: "hi", page: "/overview" })
      .catch((caught) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(409);
    expect(error.code).toBe("turn_live");
    expect(error.message).toBe("a turn is already running");
  });

  it("sends a message with its page, and the cookie that says whose conversation it is", async () => {
    fetchMock.mockResolvedValue(respond(202, { turn_id: 9 }));
    await expect(
      api.sendAssistantMessage(7, { text: "which tickets are blocked?", page: "/overview" }),
    ).resolves.toEqual({ turn_id: 9 });
    const { url, init } = lastCall();
    expect(url).toMatch(/\/api\/assistant\/conversations\/7\/messages$/);
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    expect(JSON.parse(String(init.body))).toEqual({
      text: "which tickets are blocked?",
      page: "/overview",
    });
  });

  it("opens the stream with the cookie and the last event seen", async () => {
    const body = new ReadableStream<Uint8Array>();
    fetchMock.mockResolvedValue(new Response(body, { status: 200 }));
    const signal = new AbortController().signal;
    await expect(api.openConversationStream(7, 12, signal)).resolves.toBe(body);
    const { url, init } = lastCall();
    expect(url).toMatch(/\/api\/assistant\/conversations\/7\/stream$/);
    expect(init.credentials).toBe("include");
    expect(init.signal).toBe(signal);
    expect(init.headers.Authorization).toBeUndefined();
    expect(init.headers.Accept).toBe("text/event-stream");
    expect(init.headers["Last-Event-ID"]).toBe("12");
  });

  it("opens a stream with nothing seen yet without a Last-Event-ID", async () => {
    fetchMock.mockResolvedValue(new Response(new ReadableStream(), { status: 200 }));
    await api.openConversationStream(7, 0, new AbortController().signal);
    expect(lastCall().init.headers["Last-Event-ID"]).toBeUndefined();
  });

  it("throws the detail of a stream the API refuses", async () => {
    fetchMock.mockResolvedValue(respond(404, { detail: "no such conversation" }));
    const error = await api
      .openConversationStream(7, 0, new AbortController().signal)
      .catch((caught) => caught);
    expect(error.status).toBe(404);
    expect(error.message).toBe("no such conversation");
  });
});

describe("auth same-origin", () => {
  it("fetches the prefix's /auth on this origin so the API or Vite proxy can answer", async () => {
    fetchMock.mockResolvedValue(respond(200, { google: false, oidc: true, oidc_label: "Okta" }));
    await api.authProviders();
    expect(lastCall().url).toBe("/factorial/auth/providers");
  });
});
