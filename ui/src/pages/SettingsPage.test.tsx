// @vitest-environment happy-dom
/** The Settings page, clicked for real against a mocked API. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../api/client";
import { holding } from "../api/organization.fixture";
import type { AssistantSettingsOut, ChatItems } from "../api/types";
import { sseBody } from "../components/assistant/testFakes";
import { SideNav } from "../components/shell/SideNav";
import { byText, choose, click, type Mounted, mount, settle, type } from "../testDom";
import SettingsPage from "./SettingsPage";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.restoreAllMocks();
  vi.useRealTimers();
});

const DEFAULTS: AssistantSettingsOut = {
  settings: {
    enabled: false,
    backend: "claude_sdk",
    auth_mode: "api_key",
    model: "claude-opus-5-5",
    effort: "medium",
    turn_budget_usd: "1.00",
    max_steps: 25,
    daily_budget_usd: "20.00",
    organization_daily_budget_usd: null,
    bedrock_region: "us-gov-west-1",
  },
  credentials: [],
  ready_to_send: false,
  backends: [
    {
      kind: "claude_sdk",
      label: "Claude Agent SDK",
      auth_modes: ["api_key", "subscription"],
      default_models: { api_key: "claude-opus-5-5", subscription: "claude-opus-5-5" },
    },
    {
      kind: "codex_cli",
      label: "Codex",
      auth_modes: ["api_key", "subscription", "bedrock"],
      default_models: {
        api_key: "gpt-5.3-codex",
        subscription: "gpt-5.6-terra",
        bedrock: "us.xai.grok-4.6",
      },
    },
  ],
  status: null,
};

async function page(over: Partial<AssistantSettingsOut> = {}) {
  vi.spyOn(api, "getAssistantSettings").mockResolvedValue({ ...DEFAULTS, ...over });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  view = await mount(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/settings"]}>
        <SettingsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await settle();
  return view.container;
}

function control(root: HTMLElement, label: string): HTMLElement {
  for (const element of root.querySelectorAll("label")) {
    if (element.textContent?.replace("*", "").trim() === label && element.htmlFor) {
      const found = root.querySelector<HTMLElement>(`#${CSS.escape(element.htmlFor)}`);
      if (found) return found;
    }
  }
  throw new Error(`no field labelled ${label}`);
}

function checkbox(root: HTMLElement, label: string): HTMLInputElement {
  for (const element of root.querySelectorAll("label")) {
    if (element.textContent?.trim() === label) {
      const input = element.querySelector<HTMLInputElement>('input[type="checkbox"]');
      if (input) return input;
    }
  }
  throw new Error(`no checkbox labelled ${label}`);
}

const SET_BY_OPERATOR = {
  backend: "claude_sdk",
  auth_mode: "api_key",
  updated_by: "operator@example.com",
  updated_at: "2026-10-01T09:00:00Z",
};

/** The page with these settings saved, the rest as the defaults. */
function saved(over: Partial<AssistantSettingsOut["settings"]>): Partial<AssistantSettingsOut> {
  return { settings: { ...DEFAULTS.settings, ...over } };
}

function radio(root: HTMLElement, label: string): HTMLInputElement {
  for (const element of root.querySelectorAll("label")) {
    if (element.textContent?.trim() === label) {
      const input = element.querySelector<HTMLInputElement>('input[type="radio"]');
      if (input) return input;
    }
  }
  throw new Error(`no choice labelled ${label}`);
}

describe("the credential", () => {
  it("is never shown, only who set it and when", async () => {
    const root = await page({ credentials: [SET_BY_OPERATOR] });
    expect(root.textContent).toContain("Set by operator@example.com");
    expect(root.querySelector('input[type="password"]')).toBeNull();
    expect(byText(root, "button", "Replace")).not.toBeNull();
    expect(byText(root, "button", "Remove")).not.toBeNull();
  });

  it("is sent once and the field empties", async () => {
    const store = vi.spyOn(api, "setAssistantCredential").mockResolvedValue(undefined);
    const root = await page();
    const input = control(root, "Anthropic API key") as HTMLInputElement;
    expect(input.type).toBe("password");
    await type(input, "sk-ant-secret");
    await click(byText(root, "button", "Save credential"));
    await settle();
    expect(store).toHaveBeenCalledExactlyOnceWith("claude_sdk", "api_key", "sk-ant-secret");
    expect((control(root, "Anthropic API key") as HTMLInputElement).value).toBe("");
    expect(root.textContent).not.toContain("sk-ant-secret");
  });

  it("is removed only after the confirmation", async () => {
    const clear = vi.spyOn(api, "clearAssistantCredential").mockResolvedValue(undefined);
    const root = await page({ credentials: [SET_BY_OPERATOR] });
    await click(byText(root, "button", "Remove"));
    expect(clear).not.toHaveBeenCalled();
    const popup = document.querySelector<HTMLElement>('[role="dialog"]');
    await click(byText(popup as HTMLElement, "button", "Remove credential"));
    await settle();
    expect(clear).toHaveBeenCalledExactlyOnceWith("claude_sdk", "api_key");
  });

  it("asks to save a backend switch before its credential", async () => {
    const root = await page({ credentials: [SET_BY_OPERATOR] });
    await choose(control(root, "Backend"), "codex_cli");
    expect(root.querySelector('input[type="password"]')).toBeNull();
    expect(root.textContent).not.toContain("Set by operator@example.com");
    expect(root.textContent).toContain("Save the backend, then save its credential.");
    await choose(control(root, "Backend"), "claude_sdk");
    expect(root.textContent).toContain("Set by operator@example.com");
  });

  it("empties a typed credential when the backend changes", async () => {
    const root = await page();
    await type(control(root, "Anthropic API key"), "sk-ant-typed");
    await choose(control(root, "Backend"), "codex_cli");
    await choose(control(root, "Backend"), "claude_sdk");
    expect((control(root, "Anthropic API key") as HTMLInputElement).value).toBe("");
  });

  it("stores the credential for the saved backend and sign-in", async () => {
    const store = vi.spyOn(api, "setAssistantCredential").mockResolvedValue(undefined);
    const root = await page(saved({ backend: "codex_cli", model: "gpt-5.3-codex" }));
    await type(control(root, "OpenAI API key"), "sk-openai");
    await click(byText(root, "button", "Save credential"));
    await settle();
    expect(store).toHaveBeenCalledExactlyOnceWith("codex_cli", "api_key", "sk-openai");
  });

  it("lists the other stored credentials, each removable after a confirmation", async () => {
    const clear = vi.spyOn(api, "clearAssistantCredential").mockResolvedValue(undefined);
    const other = {
      backend: "codex_cli",
      auth_mode: "subscription",
      updated_by: "admin@example.com",
      updated_at: "2026-09-01T09:00:00Z",
    };
    const root = await page({ credentials: [SET_BY_OPERATOR, other] });
    const row = [...root.querySelectorAll("li")].find((item) =>
      item.textContent?.includes("Codex · Subscription"),
    );
    expect(row?.textContent).toContain("Set by admin@example.com");
    await click(byText(row as HTMLElement, "button", "Remove"));
    expect(clear).not.toHaveBeenCalled();
    const popup = document.querySelector<HTMLElement>('[role="dialog"]');
    await click(byText(popup as HTMLElement, "button", "Remove credential"));
    await settle();
    expect(clear).toHaveBeenCalledExactlyOnceWith("codex_cli", "subscription");
  });

  it("shows no credential field for Bedrock", async () => {
    const root = await page(
      saved({ backend: "codex_cli", auth_mode: "bedrock", model: "us.xai.grok-4.6" }),
    );
    expect(root.querySelector('input[type="password"]')).toBeNull();
    expect(root.textContent).toContain("short-lived Bedrock token");
  });
});

describe("the fields", () => {
  it("says under a Claude subscription where the token comes from", async () => {
    const root = await page(saved({ auth_mode: "subscription" }));
    expect(control(root, "Claude subscription token")).not.toBeNull();
    expect(root.textContent).toContain("claude setup-token");
    expect(root.textContent).not.toContain("auth.json");
    expect(root.textContent).toContain("counts against its owner's plan and limits");
  });

  it("asks a Codex subscription for its auth.json, not a Claude token", async () => {
    const root = await page(
      saved({ backend: "codex_cli", auth_mode: "subscription", model: "gpt-5.6-terra" }),
    );
    expect(control(root, "Codex auth.json")).not.toBeNull();
    expect(root.textContent).toContain("~/.codex/auth.json");
    expect(root.textContent).toContain("codex login");
    expect(root.textContent).not.toContain("claude setup-token");
    expect(root.textContent).toContain("counts against its owner's plan and limits");
  });

  it("shows the server's message for an alias model under the field", async () => {
    vi.spyOn(api, "saveAssistantSettings").mockRejectedValue(
      new ApiError(422, "model: 'opus' is an alias; use a concrete model id"),
    );
    const root = await page();
    const model = control(root, "Model") as HTMLInputElement;
    await type(model, "opus");
    await click(byText(root, "button", "Save"));
    await settle();
    const message = document.getElementById(model.getAttribute("aria-describedby") ?? "");
    expect(model.getAttribute("aria-invalid")).toBe("true");
    expect(message?.textContent).toContain("'opus' is an alias; use a concrete model id");
  });

  it("sends every field as the spec's table names it", async () => {
    const save = vi.spyOn(api, "saveAssistantSettings").mockResolvedValue(DEFAULTS.settings);
    const root = await page();
    await click(checkbox(root, "Enabled"));
    await click(radio(root, "Subscription"));
    await type(control(root, "Model"), "claude-sonnet-4-5");
    await click(radio(root, "High"));
    await type(control(root, "Budget per reply (USD)"), "2.50");
    await type(control(root, "Model steps per reply"), "40");
    await type(control(root, "Daily budget per person (USD)"), "30.00");
    await type(control(root, "Daily budget for the organization (USD)"), "150.00");
    await click(byText(root, "button", "Save"));
    await settle();
    expect(save).toHaveBeenCalledExactlyOnceWith({
      enabled: true,
      backend: "claude_sdk",
      auth_mode: "subscription",
      model: "claude-sonnet-4-5",
      effort: "high",
      turn_budget_usd: "2.50",
      max_steps: 40,
      daily_budget_usd: "30.00",
      organization_daily_budget_usd: "150.00",
      // Only a Bedrock backend shows the field; the rest send back what the server holds.
      bedrock_region: "us-gov-west-1",
    });
  });

  it("sends the defaults the spec's table names when nothing is changed but Enabled", async () => {
    const save = vi.spyOn(api, "saveAssistantSettings").mockResolvedValue(DEFAULTS.settings);
    const root = await page();
    await click(checkbox(root, "Enabled"));
    await click(byText(root, "button", "Save"));
    await settle();
    expect(save).toHaveBeenCalledWith({ ...DEFAULTS.settings, enabled: true });
  });

  it("sends no organization budget when its field is emptied", async () => {
    const save = vi.spyOn(api, "saveAssistantSettings").mockResolvedValue(DEFAULTS.settings);
    const root = await page(saved({ organization_daily_budget_usd: "150.00" }));
    await type(control(root, "Daily budget for the organization (USD)"), "");
    await click(byText(root, "button", "Save"));
    await settle();
    expect(save).toHaveBeenCalledWith({
      ...DEFAULTS.settings,
      organization_daily_budget_usd: null,
    });
  });

  it("says on a subscription that the budgets are estimates at API prices", async () => {
    const root = await page();
    expect(root.textContent).not.toContain("at API prices");
    await click(radio(root, "Subscription"));
    expect(root.textContent).toContain(
      "On a subscription, these budgets count each reply at API prices, as the SDK estimates them.",
    );
  });

  it("says on Codex that the budgets don't limit it, since it reports no cost", async () => {
    const root = await page(saved({ backend: "codex_cli", model: "gpt-5.3-codex" }));
    expect(root.textContent).toContain("Codex reports no cost, so these budgets don't limit it.");
    await click(radio(root, "Subscription"));
    expect(root.textContent).not.toContain("at API prices");
  });

  it("restores the saved model when the saved backend comes back", async () => {
    const root = await page(
      saved({ backend: "codex_cli", auth_mode: "subscription", model: "gpt-5.7-terra" }),
    );
    await choose(control(root, "Backend"), "claude_sdk");
    await choose(control(root, "Backend"), "codex_cli");
    expect((control(root, "Model") as HTMLInputElement).value).toBe("gpt-5.7-terra");
    expect(root.textContent).not.toContain("Unsaved changes");
  });

  it("says Saved once a change is saved and read back", async () => {
    const changed = { ...DEFAULTS.settings, enabled: true };
    vi.spyOn(api, "saveAssistantSettings").mockResolvedValue(changed);
    const root = await page();
    vi.mocked(api.getAssistantSettings).mockResolvedValue({ ...DEFAULTS, settings: changed });
    await click(checkbox(root, "Enabled"));
    expect(byText(root, '[role="status"]', "Saved")).toBeNull();
    await click(byText(root, "button", "Save"));
    await settle();
    expect(checkbox(root, "Enabled").checked).toBe(true);
    expect(byText(root, '[role="status"]', "Saved")).not.toBeNull();
  });

  it("says plainly that the assistant isn't working, with the check's own words behind Details", async () => {
    const root = await page({
      status: { ok: false, message: "claude_sdk: drift", version: "x", at: "x" },
    });
    expect(root.textContent).toContain(
      "The assistant isn't working: its last check of the model provider failed.",
    );
    expect(byText(root, "button", "Show details")).not.toBeNull();
    expect(root.querySelector("code[hidden]")?.textContent).toBe("claude_sdk: drift");
  });

  it("takes the new backend's default model when the backend changes", async () => {
    const root = await page();
    await choose(control(root, "Backend"), "codex_cli");
    expect((control(root, "Model") as HTMLInputElement).value).toBe("gpt-5.3-codex");
    expect(radio(root, "API key").checked).toBe(true);
  });

  it("takes the sign-in's default when the model is still the old default", async () => {
    const root = await page(saved({ backend: "codex_cli", model: "gpt-5.3-codex" }));
    await click(radio(root, "Subscription"));
    expect((control(root, "Model") as HTMLInputElement).value).toBe("gpt-5.6-terra");
  });

  it("keeps a model the person typed when only the sign-in changes", async () => {
    const root = await page(
      saved({ backend: "codex_cli", auth_mode: "subscription", model: "gpt-5.6-terra" }),
    );
    await type(control(root, "Model"), "gpt-5.7-terra");
    await click(radio(root, "API key"));
    expect((control(root, "Model") as HTMLInputElement).value).toBe("gpt-5.7-terra");
  });

  it("picks a sign-in the new backend offers", async () => {
    const root = await page(
      saved({ backend: "codex_cli", auth_mode: "bedrock", model: "us.xai.grok-4.6" }),
    );
    await choose(control(root, "Backend"), "claude_sdk");
    expect(radio(root, "API key").checked).toBe(true);
    expect((control(root, "Model") as HTMLInputElement).value).toBe("claude-opus-5-5");
  });

  it("marks a saved sign-in the server no longer offers, until someone switches away", async () => {
    const root = await page({
      ...saved({ auth_mode: "subscription" }),
      offered: ["claude_sdk:api_key", "codex_cli:api_key"],
    });
    expect(root.textContent).toContain("No longer offered");
    radio(root, "API key").click();
    await settle();
    expect(root.textContent).not.toContain("No longer offered");
  });

  it("marks nothing when the server sends no offer list", async () => {
    const root = await page(saved({ auth_mode: "subscription" }));
    expect(root.textContent).not.toContain("No longer offered");
  });

  it("names the chosen backend and sign-in's default under Model", async () => {
    const root = await page(saved({ backend: "codex_cli", model: "gpt-5.3-codex" }));
    const model = control(root, "Model");
    const helper = document.getElementById(model.getAttribute("aria-describedby") ?? "");
    expect(helper?.textContent).toContain("such as gpt-5.3-codex");
  });

  it("offers the Backend choices from the server's vocabulary", async () => {
    const root = await page();
    const backend = control(root, "Backend") as HTMLSelectElement;
    expect([...backend.options].map((option) => option.textContent)).toEqual([
      "Claude Agent SDK",
      "Codex",
    ]);
    await choose(backend, "claude_sdk");
  });
});

function reply(text: string, rev = 2): ChatItems["items"][number] {
  return { id: 2, turn_id: 9, kind: "assistant_text", payload: { text }, final: true, rev };
}

const TURN = {
  id: 9,
  status: "done" as const,
  stop_reason: "end_turn",
  failure_reason: null,
  failure_message: null,
  cost_usd: "0.0312",
  cost_source: "estimated",
};

/** The test conversation's stream: a body that sends nothing unless the test writes to it. */
function streamed() {
  const body = sseBody();
  const open = vi.spyOn(api, "openConversationStream").mockResolvedValue(body.body);
  return { body, open };
}

describe("Test", () => {
  it("shows the reply, the latency and the estimated cost", async () => {
    vi.spyOn(api, "testAssistant").mockResolvedValue({ conversation_id: 7 });
    const items = vi
      .spyOn(api, "getAssistantItems")
      .mockResolvedValue({ items: [], turn: { ...TURN, status: "running", cost_usd: null } });
    const stream = streamed();
    const root = await page();
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
    await click(byText(root, "button", "Test"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    for (const item of [reply("O", 3), reply("OK", 4), { ...reply("stale"), rev: 1 }]) {
      stream.body.item(item);
    }
    stream.body.turn(TURN);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(items).toHaveBeenCalledWith(7);
    expect(stream.open).toHaveBeenCalledWith(7, 0, expect.any(AbortSignal));
    expect(root.textContent).toContain("Reply OK");
    expect(root.textContent).not.toContain("stale");
    expect(root.textContent).toContain("Answered in 1.0s");
    expect(root.textContent).toContain("Cost ~$0.03");
    expect(root.textContent).not.toContain("estimated");
  });

  it("shows the server's reason when the send path refuses", async () => {
    vi.spyOn(api, "testAssistant").mockRejectedValue(
      new ApiError(409, "Set up the assistant in Assistant settings."),
    );
    const root = await page();
    await click(byText(root, "button", "Test"));
    await settle();
    expect(root.querySelector('[role="alert"]')?.textContent).toBe(
      "Set up the assistant in Assistant settings.",
    );
  });

  it("shows why a turn failed instead of a reply", async () => {
    vi.spyOn(api, "testAssistant").mockResolvedValue({ conversation_id: 7 });
    streamed();
    vi.spyOn(api, "getAssistantItems").mockResolvedValue({
      items: [],
      turn: { ...TURN, status: "failed", failure_message: "the credential was refused" },
    });
    const root = await page();
    await click(byText(root, "button", "Test"));
    await settle();
    expect(root.querySelector('[role="alert"]')?.textContent).toBe("the credential was refused");
  });

  it("gives up after a minute without an end to the turn", async () => {
    vi.spyOn(api, "testAssistant").mockResolvedValue({ conversation_id: 7 });
    streamed();
    vi.spyOn(api, "getAssistantItems").mockResolvedValue({
      items: [],
      turn: { ...TURN, status: "queued", cost_usd: null },
    });
    const root = await page();
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
    await click(byText(root, "button", "Test"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(61_000);
    });
    expect(root.querySelector('[role="alert"]')?.textContent).toContain("within a minute");
  });
});

function Where() {
  return <p data-testid="where">{useLocation().pathname}</p>;
}

describe("the side navigation", () => {
  it("reaches the assistant's settings and MCP access under Settings, not the account menu", async () => {
    const client = new QueryClient();
    holding(client);
    view = await mount(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/overview"]}>
          <SideNav mode="full" onClose={() => {}} sessionEmail="operator@example.com" />
          <Routes>
            <Route path="*" element={<Where />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await click(byText(view.container, "button", "Settings"));
    expect(byText(view.container, "a", "MCP access")?.getAttribute("href")).toBe("/account/mcp");
    await click(byText(view.container, "a", "Assistant settings"));
    expect(document.querySelector('[data-testid="where"]')?.textContent).toBe("/settings");

    await click(byText(view.container, '[aria-haspopup="menu"]', "Account"));
    const items = [...document.body.querySelectorAll('[role="menuitem"]')].map((item) =>
      item.textContent?.trim(),
    );
    expect(items).toEqual(["Sign out"]);
  });
});
