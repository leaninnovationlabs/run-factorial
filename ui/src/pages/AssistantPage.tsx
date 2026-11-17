/**
 * The Factorial assistant, as a page of its own: the conversation fills the window below the top
 * bar, its history in the header, the composer at its foot (`AssistantChat`). What has to outlive
 * a visit — the open conversation, where it was scrolled, the page the reader came from — is the
 * provider's, so Show me can leave this page and Back to chat return to it as it was.
 */

import { AssistantChat } from "../components/assistant/AssistantChat";
import { useAssistantSession } from "../components/assistant/AssistantProvider";
import { usePageTitle } from "../components/ui";

export function AssistantPage() {
  const session = useAssistantSession();
  usePageTitle(["Assistant"]);
  return <AssistantChat session={session} />;
}
