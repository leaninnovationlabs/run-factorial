/**
 * The assistant's credential, write-only. The page never has the value: it knows only who stored
 * one and when. A typed value is sent once and the field is emptied at once, whatever the answer,
 * so a secret does not sit in the page waiting for a retry.
 */

import { useState } from "react";
import type { AssistantCredential } from "../../api/types";
import { ActionError, Button, ConfirmPopup, Field, RelativeTime, TextField } from "../ui";

export function CredentialField({
  label,
  credential,
  isPending,
  error,
  onStore,
  onRemove,
}: {
  label: string;
  credential: AssistantCredential;
  isPending: boolean;
  error: unknown;
  onStore: (value: string) => void;
  onRemove: () => void;
}) {
  const [isReplacing, setReplacing] = useState(false);
  const [value, setValue] = useState("");
  const editing = !credential.set || isReplacing;

  function cancel() {
    setValue("");
    setReplacing(false);
  }

  function store() {
    const typed = value.trim();
    if (!typed) return;
    setValue("");
    setReplacing(false);
    onStore(typed);
  }

  return (
    <div className="space-y-2" data-assist-anchor="settings.credential">
      {editing ? (
        // One label and one helper for the input and its buttons, so the buttons sit on the
        // input's line rather than level with the helper under it.
        <Field label={label} helperMessage="Stored encrypted. You can't view it again.">
          {(control) => (
            <div className="flex items-center gap-2">
              <div className="min-w-0 flex-1">
                <TextField
                  id={control.id}
                  aria-describedby={control["aria-describedby"]}
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  isMonospaced
                  value={value}
                  onChange={(event) => setValue(event.target.value)}
                />
              </div>
              <Button appearance="primary" isLoading={isPending} onClick={store}>
                Save credential
              </Button>
              {credential.set && (
                <Button appearance="subtle" onClick={cancel}>
                  Cancel
                </Button>
              )}
            </div>
          )}
        </Field>
      ) : (
        <div className="space-y-1">
          <span className="block text-body-sm font-semibold text-subtle">{label}</span>
          <div className="flex flex-wrap items-center gap-2 text-body text-default">
            <span>
              Set by {credential.updated_by ?? "someone"}{" "}
              <RelativeTime iso={credential.updated_at} />
            </span>
            <Button spacing="compact" onClick={() => setReplacing(true)}>
              Replace
            </Button>
            <ConfirmPopup
              label="Remove"
              name="Remove credential"
              title="Remove the credential?"
              confirmLabel="Remove credential"
              consequence="The assistant can't reply until you store a new one."
              appearance="danger"
              spacing="compact"
              resetKey="credential"
              isPending={isPending}
              onConfirm={onRemove}
            />
          </div>
        </div>
      )}
      <ActionError error={error} />
    </div>
  );
}
