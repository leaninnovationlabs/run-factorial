// @vitest-environment happy-dom
/**
 * The Secrets table's editing: a missing key's row opened for a value, a row added, a stored row
 * changed in place, and what each sends.
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

/** The page with the missing KEY's row open for a value, as its Set value opens it. */
async function show(detail: ProjectDetail = project()): Promise<HTMLElement> {
  const page = await mounted(detail);
  await click(page.querySelector(`[data-set-value="${KEY}"]`));
  if (!page.querySelector("[data-editor]")) throw new Error("no editor on the key's row");
  return page;
}

function field(scope: HTMLElement, selector: string): HTMLInputElement | HTMLSelectElement {
  const found = scope.querySelector<HTMLInputElement | HTMLSelectElement>(selector);
  if (!found) throw new Error(`no ${selector}`);
  return found;
}

describe("the sandbox delivery choice", () => {
  it("supplies every secret, as an environment variable unless a header is chosen", async () => {
    const page = await show();
    const delivery = field(page, "select[data-delivery]");
    expect([...delivery.querySelectorAll("option")].map((o) => o.value)).toEqual(["env", "header"]);
    expect(delivery.value).toBe("env");
  });

  it("offers no vendor presets, only the project's own egress hosts", async () => {
    const page = await show();
    await choose(field(page, "select[data-delivery]"), "header");
    expect(page.querySelector("select[data-preset]")).toBeNull();
    const hosts = [...field(page, "[data-egress-host]").querySelectorAll("option")];
    expect(hosts.map((option) => option.value)).toEqual(["", "api.example.com"]);
  });

  it("sends a header delivery's host and header with the value", async () => {
    const put = vi.spyOn(api, "putSecret").mockResolvedValue({ name: KEY });
    const page = await show();
    await type(field(page, 'input[type="password"]'), "rw-token");
    await choose(field(page, "select[data-delivery]"), "header");
    await choose(field(page, "[data-egress-host]"), "api.example.com");
    await type(field(page, "[data-egress-header]"), "project-access-token");

    await click(byText(page, "button", "Save"));
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
    const page = await show();
    await type(field(page, 'input[type="password"]'), "rw-token");
    await choose(field(page, "select[data-delivery]"), "header");
    await choose(field(page, "[data-egress-host]"), "api.example.com");
    await type(field(page, "[data-egress-header]"), "project-access-token");
    await click(byText(page, "button", "Save"));
    await settle(); // the rejected mutation re-renders a tick later

    const host = field(page, "[data-egress-host]");
    expect(host.getAttribute("aria-invalid")).toBe("true");
    const described = document.getElementById(host.getAttribute("aria-describedby") ?? "");
    expect(described?.textContent).toContain(refusal);
    expect(page.textContent?.split(refusal).length).toBe(2);
  });

  it("sends an environment variable when nothing else is chosen", async () => {
    const put = vi.spyOn(api, "putSecret").mockResolvedValue({ name: KEY });
    const page = await show();
    await type(field(page, 'input[type="password"]'), "rw-token");
    await click(byText(page, "button", "Save"));
    expect(put).toHaveBeenCalledWith(9042, KEY, { value: "rw-token", sandbox_delivery: "env" });
  });

  it("refuses a missing key's row without a value, and sends nothing", async () => {
    const put = vi.spyOn(api, "putSecret");
    const page = await show();
    await click(byText(page, "button", "Save"));
    expect(page.textContent).toContain("Enter a value to store.");
    expect(put).not.toHaveBeenCalled();
  });
});

describe("the choice without an egress host", () => {
  it("offers no choice, only the value, and sends an environment variable", async () => {
    const put = vi.spyOn(api, "putSecret").mockResolvedValue({ name: KEY });
    const page = await show(project({ settings: {} }));
    expect(page.querySelector("select[data-delivery]")).toBeNull();
    expect(page.querySelector('[data-editor] [data-supplied-as="env"]')).not.toBeNull();
    await type(field(page, 'input[type="password"]'), "rw-token");
    await click(byText(page, "button", "Save"));
    expect(put).toHaveBeenCalledWith(9042, KEY, { value: "rw-token", sandbox_delivery: "env" });
  });

  it("keeps the choice for a saved header, so it can be moved back", async () => {
    const page = await mounted(project({ ...ROUTED, settings: {} }));
    await click(page.querySelector(`[data-edit="${KEY}"]`));
    expect(field(page, "select[data-delivery]").value).toBe("header");
  });
});

describe("the choice with an egress host", () => {
  it("picks the project's only egress host for a header", async () => {
    const page = await show();
    await choose(field(page, "select[data-delivery]"), "header");
    expect(field(page, "[data-egress-host]").value).toBe("api.example.com");
  });
});

describe("adding a secret", () => {
  it("adds a row for a new key and sends its trimmed name, value and delivery", async () => {
    const put = vi.spyOn(api, "putSecret").mockResolvedValue({ name: "DOCS_TOKEN" });
    const page = await mounted(project());
    await click(byText(page, "button", "Add secret"));
    await type(field(page, '[data-editor] input[autocomplete="off"]'), " DOCS_TOKEN ");
    await type(field(page, 'input[type="password"]'), "docs-token");
    await click(byText(page, "button", "Save"));
    expect(put).toHaveBeenCalledWith(9042, "DOCS_TOKEN", {
      value: "docs-token",
      sandbox_delivery: "env",
    });
  });

  it("refuses a name already in the table, and sends nothing", async () => {
    const put = vi.spyOn(api, "putSecret");
    const page = await mounted(project());
    await click(byText(page, "button", "Add secret"));
    await type(field(page, '[data-editor] input[autocomplete="off"]'), KEY);
    await type(field(page, 'input[type="password"]'), "again");
    await click(byText(page, "button", "Save"));
    expect(page.textContent).toContain("That key is already in the table");
    expect(put).not.toHaveBeenCalled();
  });

  it("closes the new row on Cancel, sending nothing", async () => {
    const put = vi.spyOn(api, "putSecret");
    const page = await mounted(project());
    await click(byText(page, "button", "Add secret"));
    await click(byText(page, "button", "Cancel"));
    expect(page.querySelector("[data-editor]")).toBeNull();
    expect(byText(page, "button", "Add secret")).not.toBeNull();
    expect(put).not.toHaveBeenCalled();
  });
});

describe("a stored secret's row", () => {
  async function edit(detail: ProjectDetail): Promise<HTMLElement> {
    const page = await mounted(detail);
    expect(page.querySelector("[data-editor]")).toBeNull();
    await click(page.querySelector(`[data-edit="${KEY}"]`));
    return page;
  }

  it("changes to a header without writing the value again", async () => {
    const patch = vi
      .spyOn(api, "patchSecretDelivery")
      .mockResolvedValue({ name: KEY, mode: "header", host: "h", header: "x" });
    const put = vi.spyOn(api, "putSecret");
    const page = await edit(project(STORED));
    await choose(field(page, "select[data-delivery]"), "header");
    await type(field(page, "[data-egress-header]"), "project-access-token");
    await click(byText(page, "button", "Save"));

    expect(patch).toHaveBeenCalledWith(9042, KEY, {
      sandbox_delivery: "header",
      egress_host: "api.example.com",
      egress_header: "project-access-token",
    });
    expect(put).not.toHaveBeenCalled();
  });

  it("goes back to an environment variable without writing the value again", async () => {
    const patch = vi
      .spyOn(api, "patchSecretDelivery")
      .mockResolvedValue({ name: KEY, mode: "env", host: null, header: null });
    const put = vi.spyOn(api, "putSecret");
    const page = await edit(project(ROUTED));
    await choose(field(page, "select[data-delivery]"), "env");
    await click(byText(page, "button", "Save"));
    expect(patch).toHaveBeenCalledWith(9042, KEY, { sandbox_delivery: "env" });
    expect(put).not.toHaveBeenCalled();
  });

  it("keeps the delivery when only the value is replaced", async () => {
    const put = vi.spyOn(api, "putSecret").mockResolvedValue({ name: KEY });
    const page = await edit(project(ROUTED));
    await type(field(page, 'input[type="password"]'), "rw-rotated");
    await click(byText(page, "button", "Save"));
    expect(put).toHaveBeenCalledWith(9042, KEY, {
      value: "rw-rotated",
      sandbox_delivery: "header",
      egress_host: "api.example.com",
      egress_header: "project-access-token",
    });
  });

  it("sends nothing when Save is pressed with nothing changed", async () => {
    const put = vi.spyOn(api, "putSecret");
    const patch = vi.spyOn(api, "patchSecretDelivery");
    const page = await edit(project(STORED));
    await click(byText(page, "button", "Save"));
    expect(put).not.toHaveBeenCalled();
    expect(patch).not.toHaveBeenCalled();
    expect(page.querySelector("[data-editor]")).toBeNull();
  });
});

describe("removing a stored secret", () => {
  it("deletes it once the dialog is confirmed", async () => {
    const remove = vi.spyOn(api, "deleteSecret").mockResolvedValue(undefined);
    const page = await mounted(project(STORED));
    await click(page.querySelector(`[data-remove="${KEY}"]`));
    await settle(20);
    expect(remove).not.toHaveBeenCalled();
    const dialog = document.querySelector('[role="dialog"]');
    if (!dialog) throw new Error("no confirm dialog");
    await click(byText(dialog, "button", "Remove"));
    expect(remove).toHaveBeenCalledWith(9042, KEY);
  });
});
