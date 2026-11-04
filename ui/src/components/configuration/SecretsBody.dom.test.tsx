// @vitest-environment happy-dom
/**
 * The per-secret choice of how a cloud sandbox run receives a key: what the form sends.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../../api/client";
import { holding } from "../../api/organization.fixture";
import type { ProjectDetail } from "../../api/types";
import { byText, choose, click, type Mounted, mount, settle, type } from "../../testDom";
import { SecretsBody } from "./SecretsBody";

const KEY = "LOGS_API_TOKEN";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.restoreAllMocks();
});

function project(overrides: Partial<ProjectDetail> = {}): ProjectDetail {
  return {
    id: 9042,
    name: "widgets",
    github_repo: "acme/widgets",
    settings: { egress_allowlist: ["api.example.com"] },
    ticket_source: null,
    secret_names: [],
    declared_secrets: [KEY],
    runtime_provided_secrets: [],
    secret_deliveries: [],
    blockers: [],
    warnings: [],
    readiness: [],
    ...overrides,
  } as unknown as ProjectDetail;
}

const STORED: Partial<ProjectDetail> = {
  secret_names: [KEY],
  secret_deliveries: [{ name: KEY, mode: "env", host: null, header: null }],
};

const ROUTED: Partial<ProjectDetail> = {
  secret_names: [KEY],
  secret_deliveries: [
    { name: KEY, mode: "header", host: "api.example.com", header: "project-access-token" },
  ],
};

async function mounted(detail: ProjectDetail): Promise<HTMLElement> {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  holding(qc);
  view = await mount(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SecretsBody project={detail} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return view.container;
}

async function show(): Promise<HTMLElement> {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  holding(qc);
  view = await mount(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SecretsBody project={project()} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  const form = view.container.querySelector<HTMLElement>(`[data-store-for="${KEY}"]`);
  if (!form) throw new Error("no store form for the key");
  return form;
}

function field(form: HTMLElement, selector: string): HTMLInputElement | HTMLSelectElement {
  const found = form.querySelector<HTMLInputElement | HTMLSelectElement>(selector);
  if (!found) throw new Error(`no ${selector}`);
  return found;
}

describe("the sandbox delivery choice", () => {
  it("supplies every secret, as an environment variable unless a header is chosen", async () => {
    const form = await show();
    const delivery = field(form, "select[data-delivery]");
    expect([...delivery.querySelectorAll("option")].map((o) => o.value)).toEqual(["env", "header"]);
    expect(delivery.value).toBe("env");
    expect(form.textContent).toContain("The agent can read this value");
    await choose(delivery, "header");
    expect(form.textContent).toContain("the agent sees a placeholder");
  });

  it("offers no vendor presets, only the project's own egress hosts", async () => {
    const form = await show();
    await choose(field(form, "select[data-delivery]"), "header");
    expect(form.querySelector("select[data-preset]")).toBeNull();
    const hosts = [...field(form, "[data-egress-host]").querySelectorAll("option")];
    expect(hosts.map((option) => option.value)).toEqual(["", "api.example.com"]);
  });

  it("sends a header delivery's host and header with the value", async () => {
    const put = vi.spyOn(api, "putSecret").mockResolvedValue({ name: KEY });
    const form = await show();
    await type(field(form, 'input[type="password"]'), "rw-token");
    await choose(field(form, "select[data-delivery]"), "header");
    await choose(field(form, "[data-egress-host]"), "api.example.com");
    await type(field(form, "[data-egress-header]"), "project-access-token");

    await click(byText(form, "button", "Save"));
    expect(put).toHaveBeenCalledWith(9042, KEY, {
      value: "rw-token",
      sandbox_delivery: "header",
      egress_host: "api.example.com",
      egress_header: "project-access-token",
    });
  });

  it("shows the server's host refusal on the Egress host field", async () => {
    const refusal = "sentry.io is not one of this project's egress hosts: add it first";
    vi.spyOn(api, "putSecret").mockRejectedValue(new ApiError(422, refusal, "egress_host"));
    const form = await show();
    await type(field(form, 'input[type="password"]'), "rw-token");
    await choose(field(form, "select[data-delivery]"), "header");
    await choose(field(form, "[data-egress-host]"), "api.example.com");
    await type(field(form, "[data-egress-header]"), "project-access-token");
    await click(byText(form, "button", "Save"));
    await settle(); // the rejected mutation re-renders a tick later

    const host = field(form, "[data-egress-host]");
    expect(host.getAttribute("aria-invalid")).toBe("true");
    const described = document.getElementById(host.getAttribute("aria-describedby") ?? "");
    expect(described?.textContent).toContain(refusal);
    expect(form.textContent?.split(refusal).length).toBe(2);
  });

  it("sends an environment variable when nothing else is chosen", async () => {
    const put = vi.spyOn(api, "putSecret").mockResolvedValue({ name: KEY });
    const form = await show();
    await type(field(form, 'input[type="password"]'), "rw-token");
    await click(byText(form, "button", "Save"));
    expect(put).toHaveBeenCalledWith(9042, KEY, { value: "rw-token", sandbox_delivery: "env" });
  });

  it("sends an environment variable choice with no host or header", async () => {
    const put = vi.spyOn(api, "putSecret").mockResolvedValue({ name: KEY });
    const form = await show();
    await type(field(form, 'input[type="password"]'), "db-password");
    await choose(field(form, "select[data-delivery]"), "env");
    expect(form.textContent).toContain("The agent can read this value");
    await click(byText(form, "button", "Save"));
    expect(put).toHaveBeenCalledWith(9042, KEY, { value: "db-password", sandbox_delivery: "env" });
  });
});

describe("the dropdown's guidance", () => {
  it("offers the header only once the project has an egress host", async () => {
    const page = await mounted(project({ settings: {} }));
    const header = page.querySelector<HTMLOptionElement>(
      `[data-store-for="${KEY}"] select[data-delivery] option[value="header"]`,
    );
    expect(header?.disabled).toBe(true);
    expect(header?.textContent).toContain("add an egress host first");
  });

  it("picks the project's only egress host for a header", async () => {
    const form = await show();
    await choose(field(form, "select[data-delivery]"), "header");
    expect(field(form, "[data-egress-host]").value).toBe("api.example.com");
  });

  it("marks the environment variable's consequence as a warning", async () => {
    const form = await show();
    const note = form.querySelector('[data-tone="warning"]');
    expect(note?.textContent).toContain("The agent can read this value");
  });
});

describe("a stored secret's delivery", () => {
  it("changes to a header without writing the value again", async () => {
    const patch = vi
      .spyOn(api, "patchSecretDelivery")
      .mockResolvedValue({ name: KEY, mode: "header", host: "h", header: "x" });
    const put = vi.spyOn(api, "putSecret");
    const page = await mounted(project(STORED));
    const row = page.querySelector<HTMLElement>(`[data-delivery-for="${KEY}"]`);
    if (!row) throw new Error("no delivery editor on the stored row");
    expect(page.querySelector(`[data-store-for="${KEY}"]`)).toBeNull();

    await choose(field(row, "select[data-delivery]"), "header");
    await type(field(row, "[data-egress-header]"), "project-access-token");
    await click(byText(row, "button", "Save"));

    expect(patch).toHaveBeenCalledWith(9042, KEY, {
      sandbox_delivery: "header",
      egress_host: "api.example.com",
      egress_header: "project-access-token",
    });
    expect(put).not.toHaveBeenCalled();
  });

  it("goes back to an environment variable as soon as it is chosen", async () => {
    const patch = vi
      .spyOn(api, "patchSecretDelivery")
      .mockResolvedValue({ name: KEY, mode: "env", host: null, header: null });
    const page = await mounted(project(ROUTED));
    const row = page.querySelector<HTMLElement>(`[data-delivery-for="${KEY}"]`);
    if (!row) throw new Error("no delivery editor on the stored row");
    await choose(field(row, "select[data-delivery]"), "env");
    expect(patch).toHaveBeenCalledWith(9042, KEY, { sandbox_delivery: "env" });
  });

  it("keeps the delivery when only the value is replaced", async () => {
    const put = vi.spyOn(api, "putSecret").mockResolvedValue({ name: KEY });
    const page = await mounted(project(ROUTED));
    await click(byText(page, "button", "Replace value"));
    const form = page.querySelector<HTMLElement>(`[data-store-for="${KEY}"]`);
    if (!form) throw new Error("no value form");
    expect(form.querySelector("select[data-delivery]")).toBeNull();
    await type(field(form, 'input[type="password"]'), "rw-rotated");
    await click(byText(form, "button", "Save"));
    expect(put).toHaveBeenCalledWith(9042, KEY, {
      value: "rw-rotated",
      sandbox_delivery: "header",
      egress_host: "api.example.com",
      egress_header: "project-access-token",
    });
  });
});

describe("removing a stored secret", () => {
  it("deletes it once the dialog is confirmed", async () => {
    const remove = vi.spyOn(api, "deleteSecret").mockResolvedValue(undefined);
    const page = await mounted(project(STORED));
    await click(byText(page, "button", "Remove"));
    await settle(20);
    expect(remove).not.toHaveBeenCalled();
    const dialog = document.querySelector('[role="dialog"]');
    if (!dialog) throw new Error("no confirm dialog");
    await click(byText(dialog, "button", "Remove"));
    expect(remove).toHaveBeenCalledWith(9042, KEY);
  });
});
