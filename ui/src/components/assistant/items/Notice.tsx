/**
 * One line about a turn, from the assistant's process: a failure, a limit reached, a restart, or
 * the outcome of an approved proposal. `reason` is a closed word (`assistant/items.py`), and the
 * line offers what that reason calls for: Retry, which sends the turn's own message again from
 * the page it was sent on, with what it pointed at, or a link to Settings for a credential or a
 * configuration to fix. A message that carried files has no Retry: the browser no longer holds
 * their bytes and nothing serves them back, so the line says to attach them again.
 */

import { useCan } from "../../../api/organizationQueries";
import type { ChatItem } from "../../../api/types";
import { Button, Icon } from "../../ui";
import { DashboardLink } from "../AssistantProvider";
import type { Pointed } from "../draft";
import { filesIn, pointedIn } from "../PointedChips";

/** Sending the same words again changes nothing for these: a limit, a refusal, a setting. */
const NO_RETRY = new Set(["auth", "config", "refusal", "budget", "steps"]);
const TO_SETTINGS = new Set(["auth", "config"]);

/** The message the notice's turn answered, when it is one a person sent and sending it again
 *  could help. A notice with no turn is a proposal's outcome. */
type Again = { text: string; page: string; pointed: Pointed[]; hadFiles: boolean };

function retryOf(notice: ChatItem, items: ChatItem[]): Again | null {
  const reason = notice.payload.reason;
  if (typeof reason === "string" && NO_RETRY.has(reason)) return null;
  const sent = items.find((item) => item.kind === "user_text" && item.turn_id === notice.turn_id);
  if (!sent) return null;
  const text = typeof sent.payload.text === "string" ? sent.payload.text : "";
  const pointed = pointedIn(sent.payload);
  const hadFiles = filesIn(sent.payload).length > 0;
  if (!text && pointed.length === 0 && !hadFiles) return null;
  const page = sent.payload.page;
  return { text, page: typeof page === "string" ? page : "", pointed, hadFiles };
}

export function Notice({
  item,
  items,
  onRetry,
  retryDisabled = false,
}: {
  item: ChatItem;
  items: ChatItem[];
  onRetry: (text: string, page: string, pointed: Pointed[]) => void;
  retryDisabled?: boolean;
}) {
  const { text, reason } = item.payload;
  const retry = retryOf(item, items);
  const canConfigure = useCan("assistant:configure");
  const settings = canConfigure && typeof reason === "string" && TO_SETTINGS.has(reason);
  const trouble = item.turn_id !== null;
  return (
    <div className="flex items-start gap-2 text-body text-subtle">
      <Icon
        glyph={trouble ? "status-warning" : "status-information"}
        className={trouble ? "mt-0.5 text-icon-warning" : "mt-0.5 text-icon-information"}
      />
      <div className="min-w-0 flex-1 space-y-1">
        <p className="break-words text-default">{typeof text === "string" ? text : ""}</p>
        {settings && <DashboardLink to="/settings">Open the assistant settings</DashboardLink>}
        {retry?.hadFiles && <p>To try again, attach the files again and send the message.</p>}
        {retry && !retry.hadFiles && (
          <Button
            spacing="compact"
            iconBefore="refresh"
            aria-disabled={retryDisabled || undefined}
            onClick={() => onRetry(retry.text, retry.page, retry.pointed)}
          >
            Retry
          </Button>
        )}
      </div>
    </div>
  );
}
