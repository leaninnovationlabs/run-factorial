/**
 * One tool call, as one line: what the assistant read or did, and what it named, such as "Read
 * ticket acme/api#212". The result itself stays with the model; the line shows only an error.
 * The lines are listed by an open `ToolGroup`, which also borrows their words.
 *
 * A call that drew a card (a chart, a Show me, an approval card, an editor draft) has nothing to
 * add to the card it drew, so the stream leaves it out (`namesCard`). Its result names the card
 * by `card_item_id` (`mcp_server/results.py::respond`).
 */

import { Icon } from "../../ui";
import { SpinnerGlyph } from "../../ui/SpinnerGlyph";

const WORDS: Record<string, string> = {
  factorial_projects_list: "List projects",
  factorial_project_get: "Read project",
  factorial_tickets_list: "List tickets",
  factorial_ticket_get: "Read ticket",
  factorial_runs_list: "List step runs",
  factorial_metrics_get: "Read delivery metrics",
  factorial_costs_get: "Read cost per ticket",
  factorial_vocabulary_get: "Read the config vocabulary",
  factorial_config_read: "Read config",
  factorial_config_validate: "Validate config",
  factorial_reference_read: "Read a reference",
  factorial_ui_show: "Show me",
  factorial_chart: "Draw a chart",
  factorial_editor_open: "Open in the process editor",
  factorial_ticket_exit: "Propose an exit",
  factorial_ticket_reenter: "Propose a re-entry",
  factorial_ticket_interrupt: "Propose an interrupt",
  factorial_ticket_resume: "Propose a resume",
  factorial_ticket_unblock: "Propose an unblock",
  factorial_ticket_retry: "Propose a retry",
  factorial_ticket_requeue: "Propose a requeue",
  factorial_ticket_restart: "Propose a restart",
  factorial_ticket_create: "Propose a new issue",
  factorial_config_commit: "Propose a config commit",
};

/** The argument that says what a call was about, in the order a reader would name it. */
const TARGETS = ["ticket", "project", "process", "path", "name", "title"];

const TARGET_MAX = 80;

export function namesCard(summary: unknown): boolean {
  return typeof summary === "string" && /"card_item_id":\s*\d+/.test(summary);
}

export function words(name: unknown): string {
  if (typeof name !== "string") return "";
  return WORDS[name] ?? name.replace(/^factorial_/, "").replace(/_/g, " ");
}

export function failed({ status, ok }: Record<string, unknown>): boolean {
  return status === "done" && ok === false;
}

export function target(input: unknown): string | null {
  if (typeof input !== "object" || input === null) return null;
  for (const key of TARGETS) {
    const value = (input as Record<string, unknown>)[key];
    if (typeof value === "string" && value.trim()) {
      const text = value.trim();
      return text.length > TARGET_MAX ? `${text.slice(0, TARGET_MAX - 1)}…` : text;
    }
    if (typeof value === "number") return String(value);
  }
  return null;
}

export function ToolLine({ payload }: { payload: Record<string, unknown> }) {
  const { name, status, summary, input } = payload;
  const broke = failed(payload);
  const about = target(input);
  const error = broke && typeof summary === "string" ? summary.split("\n")[0] : null;
  return (
    <p className="flex items-start gap-2 text-body-sm text-subtle">
      <span className="mt-0.5 shrink-0">
        {status === "running" ? (
          <SpinnerGlyph size="small" />
        ) : broke ? (
          <Icon glyph="status-error" size="small" className="text-icon-danger" />
        ) : status === "stopped" ? (
          <Icon glyph="close" size="small" />
        ) : (
          <Icon glyph="check" size="small" />
        )}
      </span>
      <span className="min-w-0 break-words">
        {words(name)}
        {about && ` ${about}`}
        {error && <span className="block text-danger">{error}</span>}
      </span>
    </p>
  );
}
