// @vitest-environment happy-dom
/** An approval card: what the model asks to do, and the person's Approve or Decline. */

import { act } from "react";
import { afterEach, describe, expect, test, vi } from "vitest";
import { ApiError, api } from "../../../api/client";
import type { ProposalPayload } from "../../../api/types";
import { byText, click, type Mounted, settle } from "../../../testDom";
import { mountInAssistant } from "../testFakes";
import { ProposalCard } from "./ProposalCard";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.restoreAllMocks();
});

const RETRY: ProposalPayload = {
  proposal_id: 5,
  tool: "factorial_ticket_retry",
  title: "Retry a failed run now",
  preview: {
    ticket: "acme/api#212",
    ticket_id: 212,
    title: "Retry the webhook",
    step: "build",
    status: "blocked",
  },
  status: "pending",
  expires_at: "2026-10-13T10:00:00Z",
  decided_by: null,
  decided_at: null,
  result: null,
};

async function render(payload: ProposalPayload) {
  const mounted = await mountInAssistant(<ProposalCard payload={payload} />);
  view = mounted.view;
  return mounted.view.container;
}

describe("ProposalCard", () => {
  test("ProposalCard Approve posts once and shows succeeded or the refusal reason", async () => {
    const approve = vi.spyOn(api, "approveProposal").mockResolvedValueOnce({
      proposal_id: 5,
      status: "refused",
      card_item_id: 40,
      result: {
        error:
          "acme/api#212 is running, so factorial_ticket_retry is not possible now\nValid: factorial_ticket_interrupt",
      },
    });
    let root = await render(RETRY);
    expect(root.textContent).toContain("Retry a failed run now");
    expect(root.textContent).toContain("acme/api#212");
    const button = byText(root, "button", "Approve");
    await act(async () => {
      button?.click();
      button?.click();
    });
    await settle();
    expect(approve).toHaveBeenCalledExactlyOnceWith(5);
    expect(root.textContent).toContain("Refused");
    expect(root.textContent).toContain(
      "acme/api#212 is running, so factorial_ticket_retry is not possible now",
    );
    expect(byText(root, "button", "Approve")).toBeNull();
    await view?.unmount();

    approve.mockResolvedValueOnce({
      proposal_id: 5,
      status: "succeeded",
      card_item_id: 40,
      result: { ref: "acme/api#212", landed: true, ticket_status: "running" },
    });
    root = await render(RETRY);
    await click(byText(root, "button", "Approve"));
    await settle();
    expect(root.textContent).toContain("Succeeded");
    expect(root.textContent).not.toContain("Refused");
  });

  test("ProposalCard for a config commit shows the files and the diff", async () => {
    const root = await render({
      ...RETRY,
      tool: "factorial_config_commit",
      title: "Commit config",
      preview: {
        project: "api",
        base_sha: "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
        would_write: [
          { path: ".factorial/processes/process-standard.yaml", origin: "process" },
          { path: ".factorial/mcp/linear.yaml", origin: "edit" },
        ],
        diff: "--- a/.factorial/processes/process-standard.yaml\n+++ b/.factorial/processes/process-standard.yaml\n@@ -1 +1 @@\n-lanes: 1\n+lanes: 2\n",
      },
    });
    expect(root.textContent).toContain(".factorial/processes/process-standard.yaml");
    expect(root.textContent).toContain(".factorial/mcp/linear.yaml");
    const diff = root.querySelector("pre");
    expect(diff?.textContent).toContain("-lanes: 1");
    expect(diff?.textContent).toContain("+lanes: 2");
  });

  test("Decline posts and the card says so", async () => {
    const decline = vi.spyOn(api, "declineProposal").mockResolvedValue({
      proposal_id: 5,
      status: "declined",
      card_item_id: 40,
      result: null,
    });
    const root = await render(RETRY);
    await click(byText(root, "button", "Decline"));
    await settle();
    expect(decline).toHaveBeenCalledExactlyOnceWith(5);
    expect(root.textContent).toContain("Declined");
    expect(byText(root, "button", "Approve")).toBeNull();
  });

  test("a press that never reached the API can be pressed again", async () => {
    const approve = vi
      .spyOn(api, "approveProposal")
      .mockRejectedValueOnce(new ApiError(0, "cannot reach the API"))
      .mockResolvedValueOnce({ proposal_id: 5, status: "succeeded", card_item_id: 40, result: {} });
    const root = await render(RETRY);
    await click(byText(root, "button", "Approve"));
    await settle();
    expect(root.querySelector('[role="alert"]')?.textContent).toContain("cannot reach the API");
    await click(byText(root, "button", "Approve"));
    await settle();
    expect(approve).toHaveBeenCalledTimes(2);
    expect(root.textContent).toContain("Succeeded");
  });

  test("a decided proposal offers no buttons, and a late Approve says why it was refused", async () => {
    let root = await render({ ...RETRY, status: "expired" });
    expect(root.textContent).toContain("Expired");
    expect(byText(root, "button", "Approve")).toBeNull();
    await view?.unmount();

    vi.spyOn(api, "approveProposal").mockRejectedValue(
      new ApiError(410, "proposal P5 expired; ask the assistant to propose it again", "expired"),
    );
    root = await render(RETRY);
    await click(byText(root, "button", "Approve"));
    await settle();
    expect(root.querySelector('[role="alert"]')?.textContent).toContain(
      "ask the assistant to propose it again",
    );
  });

  test("a restart proposal shows the snapshot it moves to and what it removes", async () => {
    const root = await render({
      ...RETRY,
      tool: "factorial_ticket_restart",
      title: "Restart a ticket on its lane's current process",
      preview: {
        ...RETRY.preview,
        status: "running",
        process: "a1b2c3d → f0e1d2c",
        live_run: { step: "build", attempt: 2, kind: "agent" },
        parked_run: null,
        close_prs: [
          {
            number: 241,
            url: "https://github.com/acme/api/pull/241",
            head: "impl/212-hook",
            base: "spec/212-hook",
          },
        ],
        delete_branches: ["impl/212-hook", "spec/212-hook"],
        merged_outside: [
          {
            number: 230,
            url: "https://github.com/acme/api/pull/230",
            head: "spec/212-hook",
            base: "main",
          },
        ],
      },
    });
    expect(root.textContent).toContain("a1b2c3d → f0e1d2c");
    expect(root.textContent).toContain("build attempt 2");
    expect(root.querySelector('a[href="https://github.com/acme/api/pull/241"]')?.textContent).toBe(
      "#241",
    );
    const items = [...root.querySelectorAll("li")].map((li) => li.textContent);
    expect(items).toEqual(expect.arrayContaining(["impl/212-hook", "spec/212-hook"]));
    expect(root.textContent).toContain("Already merged");
    expect(root.querySelector('a[href="https://github.com/acme/api/pull/230"]')?.textContent).toBe(
      "#230",
    );
  });
});
