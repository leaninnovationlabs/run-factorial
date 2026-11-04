/**
 * A Show me card: a page of the dashboard, and the control on it the assistant points at. Only
 * the press moves anyone (`AssistantProvider`'s `showMe`): the model offers the chip, never the
 * navigation.
 */

import type { ShowMePayload } from "../../../api/types";
import { legacyTabHref } from "../../project/place";
import { Button } from "../../ui";
import { isDashboardPath, useAssistant } from "../AssistantProvider";

export function asShowMe(payload: Record<string, unknown>): ShowMePayload | null {
  const { path, tab, anchor, note } = payload;
  if (typeof path !== "string" || !isDashboardPath(path)) return null;
  const optional = (value: unknown) => (typeof value === "string" && value ? value : null);
  // A card stored before a project had pages names its page with `tab`, and its tab strip as the
  // anchor; both now live elsewhere.
  const section = anchor === "process.commit" ? "process" : null;
  return {
    path: legacyTabHref(path, tab, section) ?? path,
    anchor: anchor === "project.tabs" ? "project.nav" : optional(anchor),
    note: optional(note),
  };
}

export function ShowMeChip({ payload }: { payload: Record<string, unknown> }) {
  const { showMe } = useAssistant();
  const card = asShowMe(payload);
  if (!card) return null;
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button iconBefore="eye" onClick={() => showMe(card)}>
        Show me
      </Button>
      {card.note && <span className="min-w-0 text-body text-subtle">{card.note}</span>}
    </div>
  );
}
