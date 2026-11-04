/**
 * The create page's credentials section: the keys the chosen CLI, authentication and sandbox
 * read, the optional check that proves them before anything is stored, and the template a
 * passing sandbox check lists.
 *
 * Every secret field is uncontrolled and named by its credential, and the page reads them off
 * the form only inside its Test and Create handlers: a value is never state, a prop or an
 * attribute. The vendor field is keyed by its type, so a switch of vendor unmounts the old
 * field rather than renaming it with the typed key still inside.
 */

import type { RuntimeCheckOut, RuntimeTemplateBuild } from "../../api/types";
import {
  Button,
  ErrorMessage,
  RadioGroup,
  SectionMessage,
  Select,
  TextArea,
  TextField,
} from "../ui";
import { buildLabel, Outcome } from "./RuntimeCredentials";
import { type Choice, type CodexSignIn, deferredLogin, typeFor } from "./runtimeChoice";
import {
  type CredentialForm,
  credentialFormFor,
  credentialNameFor,
  E2B_CREDENTIAL,
  E2B_KEY_FORM,
  requiresStoredCredential,
  SUBSCRIPTION_CREDENTIAL,
} from "./runtimes";

export interface CredentialCheckState {
  onTest: () => void;
  isPending: boolean;
  result: RuntimeCheckOut | null;
  error: unknown;
}

const REQUIRED = "Required to create the runtime.";

export function CreateCredentials({
  choice,
  fields,
  codexSignIn,
  onCodexSignIn,
  onEdit,
  missing,
  templates,
  template,
  onTemplate,
  test,
}: {
  choice: Choice;
  /** The credential names Create sends. */
  fields: string[];
  codexSignIn: CodexSignIn;
  onCodexSignIn: (value: CodexSignIn) => void;
  onEdit: () => void;
  /** Credential names a refused Create found empty. */
  missing: ReadonlySet<string>;
  /** The builds the current check listed, newest first; null until a check passes. */
  templates: RuntimeTemplateBuild[] | null;
  template: string | null;
  /** Null leaves the template to the runtime's page. */
  onTemplate: (pin: string | null) => void;
  test: CredentialCheckState;
}) {
  const type = typeFor(choice.cli, choice.auth);
  const form = type === null ? null : credentialFormFor(type);
  const name = type && credentialNameFor(type);
  const cloud = choice.sandbox === "cloud";

  return (
    <div className="space-y-4">
      {form && name && (
        <SecretField
          key={type}
          name={name}
          copy={form}
          isMissing={missing.has(name)}
          onEdit={onEdit}
        />
      )}
      {type !== null && !requiresStoredCredential(type) && (
        <p className="text-body-sm text-subtle">
          No key to store: each run gets a short-lived Bedrock token from the host's AWS role. The
          region and model can be changed on the runtime's page.
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
              {
                value: "chatgpt",
                label: "Sign in with ChatGPT",
                description:
                  "Once the runtime exists, its page shows a one-time code to enter at OpenAI.",
              },
              {
                value: "paste",
                label: "Paste auth.json",
                description: "From a machine that has already signed in.",
              },
            ]}
          />
          {codexSignIn === "paste" && (
            <TextArea
              label="auth.json"
              name={SUBSCRIPTION_CREDENTIAL}
              isRequired
              minimumRows={4}
              spellCheck={false}
              autoComplete="off"
              isMonospaced
              placeholder="The contents of ~/.codex/auth.json from a signed-in machine"
              onChange={onEdit}
              errorMessage={missing.has(SUBSCRIPTION_CREDENTIAL) ? REQUIRED : undefined}
            />
          )}
        </>
      )}
      {cloud && (
        <>
          <SecretField
            name={E2B_CREDENTIAL}
            copy={E2B_KEY_FORM}
            isMissing={missing.has(E2B_CREDENTIAL)}
            onEdit={onEdit}
          />
          {templates && templates.length > 0 ? (
            <Select
              label="Template"
              value={template ?? ""}
              onChange={(event) => onTemplate(event.target.value || null)}
              helperMessage="The build each run boots. You can change it on the runtime's page."
              options={[
                { value: "", label: "Choose later on the runtime page" },
                ...templates.map((build) => ({
                  value: build.pin,
                  label: buildLabel(build, ""),
                })),
              ]}
            />
          ) : (
            <p className="text-body-sm text-subtle">
              {templates
                ? "This team has no ready builds. Build one, then choose it on the runtime's page."
                : "Test the credentials to list this team's template builds."}
            </p>
          )}
        </>
      )}
      {fields.length > 0 && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-3">
            <Button isLoading={test.isPending} onClick={test.onTest}>
              Test credentials
            </Button>
            <span className="text-body-sm text-subtle">
              Makes one small call with each key. Nothing is stored.
            </span>
          </div>
          <ErrorMessage error={test.error} />
          {test.result && (
            <div className="space-y-2">
              {deferredLogin(type, codexSignIn) ? (
                <SectionMessage title="Model provider">
                  Sign in with ChatGPT on the runtime's page once it exists.
                </SectionMessage>
              ) : (
                <Outcome
                  title={test.result.local ? "Local check" : "Model provider"}
                  check={test.result.model}
                />
              )}
              {test.result.e2b && <Outcome title="Cloud sandbox" check={test.result.e2b} />}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SecretField({
  name,
  copy,
  isMissing,
  onEdit,
}: {
  name: string;
  copy: CredentialForm;
  isMissing: boolean;
  onEdit: () => void;
}) {
  return (
    <TextField
      label={copy.label}
      name={name}
      type="password"
      isRequired
      autoComplete="off"
      spellCheck={false}
      placeholder={copy.placeholder}
      onChange={onEdit}
      helperMessage={copy.hint}
      errorMessage={isMissing ? REQUIRED : undefined}
    />
  );
}
