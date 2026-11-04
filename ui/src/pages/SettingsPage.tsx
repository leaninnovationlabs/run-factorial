/** The assistant's settings, and a test turn. Under Settings in the side navigation; the chat
 *  itself is the Assistant page. */

import { AssistantSettings } from "../components/settings/AssistantSettings";
import { PageHeader, usePageTitle } from "../components/ui";

export default function SettingsPage() {
  usePageTitle(["Assistant settings"]);
  return (
    <div className="space-y-6">
      <PageHeader
        title="Assistant settings"
        bottomBar="The assistant is a chat that can read your projects and tickets and propose changes for you to approve."
      />
      <AssistantSettings />
    </div>
  );
}
