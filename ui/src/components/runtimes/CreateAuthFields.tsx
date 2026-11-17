/**
 * The create page's Authentication step, under its cards: the model key (or the pasted
 * `auth.json`) with its own `Test key`, and the outcome of that test.
 *
 * Every secret field is uncontrolled and named by its credential (see `KeyRow`). The vendor
 * field is keyed by its type, so a switch of vendor unmounts the old field rather than renaming
 * it with the typed key still inside. A ChatGPT sign-in has nothing to test: the login happens on
 * the runtime's page, after create.
 */

import type { RuntimeCheckOut } from "../../api/types";
import { ErrorMessage, RadioGroup } from "../ui";
import { KeyRow } from "./KeyRow";
import { Outcome } from "./RuntimeCredentials";
import { type Choice, type CodexSignIn, typeFor } from "./runtimeChoice";
import { credentialFormFor, credentialNameFor, SUBSCRIPTION_CREDENTIAL } from "./runtimes";

export interface CredentialCheckState {
  onTest: () => void;
  isPending: boolean;
  result: RuntimeCheckOut | null;
  error: unknown;
}

export function CreateAuthFields({
  choice,
  codexSignIn,
  onCodexSignIn,
  onEdit,
  missing,
  test,
}: {
  choice: Choice;
  codexSignIn: CodexSignIn;
  onCodexSignIn: (value: CodexSignIn) => void;
  onEdit: () => void;
  /** Credential names a refused Create found empty. */
  missing: ReadonlySet<string>;
  /** The model key's own check: its outcome is `result.model`. */
  test: CredentialCheckState;
}) {
  const type = typeFor(choice.cli, choice.auth);
  const form = type === null ? null : credentialFormFor(type);
  const name = type && credentialNameFor(type);
  const pasting = type === "codex_subscription" && codexSignIn === "paste";
  const testable = Boolean((form && name) || pasting);
  const row = { onTest: test.onTest, isPending: test.isPending };

  return (
    <div className="space-y-4">
      {form && name && (
        <KeyRow
          key={type}
          name={name}
          label={form.label}
          placeholder={form.placeholder}
          isMissing={missing.has(name)}
          onEdit={onEdit}
          test={row}
        />
      )}
      {type === "codex_bedrock" && (
        <p className="text-body-sm text-subtle">
          No key to store. Each run gets a short-lived token from the host's AWS role.
        </p>
      )}
      {type === "codex_subscription" && (
        <>
          <RadioGroup
            label="ChatGPT sign-in"
            name="codex-sign-in"
            isRequired
            value={codexSignIn}
            onChange={(value) => onCodexSignIn(value as CodexSignIn)}
            options={[
              { value: "chatgpt", label: "Sign in with ChatGPT" },
              { value: "paste", label: "Paste auth.json" },
            ]}
          />
          {pasting && (
            <KeyRow
              name={SUBSCRIPTION_CREDENTIAL}
              label="auth.json"
              multiline
              placeholder="The contents of ~/.codex/auth.json from a signed-in machine"
              isMissing={missing.has(SUBSCRIPTION_CREDENTIAL)}
              onEdit={onEdit}
              test={row}
            />
          )}
        </>
      )}
      {testable && (
        <>
          <ErrorMessage error={test.error} />
          {test.result && (
            <Outcome
              title={test.result.local ? "Local check" : "Model provider"}
              check={test.result.model}
            />
          )}
        </>
      )}
    </div>
  );
}
