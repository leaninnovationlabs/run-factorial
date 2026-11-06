/**
 * The assistant's section of Settings: what turns it on and bounds it, its credential, and a Test
 * that runs one real turn. The fields and their defaults are the spec's table; the server holds
 * the rules (a concrete model id, the ranges), and its message for a refused field is drawn under
 * that field.
 */

import { type FormEvent, type ReactNode, useState } from "react";
import {
  useAssistantSettings,
  useClearAssistantCredential,
  useSaveAssistantSettings,
  useSetAssistantCredential,
} from "../../api/queries";
import type {
  AssistantBackend,
  AssistantSettingsOut,
  AssistantStoredCredential,
  AssistantSettings as Settings,
} from "../../api/types";
import { formatCost, formatElapsed } from "../../format";
import {
  ActionError,
  Button,
  Checkbox,
  ConfirmPopup,
  ErrorDetails,
  Lozenge,
  Panel,
  QueryBoundary,
  RadioGroup,
  RelativeTime,
  SectionMessage,
  Select,
  TextField,
} from "../ui";
import { CredentialField } from "./CredentialField";
import { useConnectionTest } from "./useConnectionTest";

const AUTH_LABELS: Record<string, string> = {
  api_key: "API key",
  subscription: "Subscription",
  bedrock: "AWS Bedrock (host role)",
};

/** What the credential field asks for, keyed `backend:auth_mode`. */
const CREDENTIAL_LABELS: Record<string, string> = {
  "claude_sdk:api_key": "Anthropic API key",
  "claude_sdk:subscription": "Claude subscription token",
  "codex_cli:api_key": "OpenAI API key",
  "codex_cli:subscription": "Codex auth.json",
};

const EFFORTS = [
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
];

/** FastAPI's 422 arrives joined as "field: message; field: message" (`api/client.ts`). */
function fieldErrors(message: string | undefined): Record<string, string> {
  const found: Record<string, string> = {};
  for (const part of message?.split(/; (?=[a-z_]+: )/) ?? []) {
    const at = part.indexOf(": ");
    if (at > 0 && /^[a-z_]+$/.test(part.slice(0, at)))
      found[part.slice(0, at)] = part.slice(at + 2);
  }
  return found;
}

type Draft = Omit<Settings, "max_steps" | "organization_daily_budget_usd"> & {
  max_steps: string;
  organization_daily_budget_usd: string;
};

const toDraft = (settings: Settings): Draft => ({
  ...settings,
  max_steps: String(settings.max_steps),
  organization_daily_budget_usd: settings.organization_daily_budget_usd ?? "",
});
const toSettings = (draft: Draft): Settings => ({
  ...draft,
  max_steps: Number(draft.max_steps),
  organization_daily_budget_usd: draft.organization_daily_budget_usd.trim() || null,
});

type Save = ReturnType<typeof useSaveAssistantSettings>;

export function AssistantSettings() {
  const query = useAssistantSettings();
  // Above the key below, so the save's outcome outlives the remount its own success causes.
  const save = useSaveAssistantSettings();
  return (
    <QueryBoundary query={query} errorTitle="Couldn't load the assistant's settings">
      {(data) => (
        // A saved change comes back as new settings and starts the draft again from them.
        <SettingsPanels key={JSON.stringify(data.settings)} data={data} save={save} />
      )}
    </QueryBoundary>
  );
}

function SettingsPanels({ data, save }: { data: AssistantSettingsOut; save: Save }) {
  const store = useSetAssistantCredential();
  const clear = useClearAssistantCredential();
  const [draft, setDraft] = useState<Draft>(() => toDraft(data.settings));
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((current) => ({ ...current, [key]: value }));

  const saved = JSON.stringify(toDraft(data.settings));
  const isDirty = JSON.stringify(draft) !== saved;
  const errors = fieldErrors(save.error?.message);
  const unplaced = save.error && Object.keys(errors).length === 0 ? save.error : null;
  const backend = data.backends.find((candidate) => candidate.kind === draft.backend);
  const modes = backend?.auth_modes ?? [draft.auth_mode];
  const defaultModel = (kind: string, mode: string) =>
    data.backends.find((candidate) => candidate.kind === kind)?.default_models[mode];

  /** A model never crosses vendors, and a typed one survives a change of sign-in alone. */
  function switchTo(kind: string, mode: string) {
    setDraft((current) => {
      const offered = data.backends.find((candidate) => candidate.kind === kind)?.auth_modes;
      const auth_mode = !offered || offered.includes(mode) ? mode : (offered[0] ?? mode);
      const keepsModel =
        kind === current.backend &&
        current.model !== defaultModel(current.backend, current.auth_mode);
      const backToSaved =
        kind !== current.backend &&
        kind === data.settings.backend &&
        auth_mode === data.settings.auth_mode;
      const model = backToSaved
        ? data.settings.model
        : keepsModel
          ? current.model
          : (defaultModel(kind, auth_mode) ?? current.model);
      return { ...current, backend: kind, auth_mode, model };
    });
  }
  const credentialLabel =
    CREDENTIAL_LABELS[`${draft.backend}:${draft.auth_mode}`] ??
    AUTH_LABELS[draft.auth_mode] ??
    draft.auth_mode;
  const isSaved = (credential: AssistantStoredCredential) =>
    credential.backend === data.settings.backend &&
    credential.auth_mode === data.settings.auth_mode;
  const savedCredential = data.credentials.find(isSaved);
  /** The server keeps a saved sign-in its list dropped; an older API sends no list. */
  const noLongerOffered =
    data.offered !== undefined &&
    draft.backend === data.settings.backend &&
    draft.auth_mode === data.settings.auth_mode &&
    !data.offered.includes(`${draft.backend}:${draft.auth_mode}`);
  const status = data.status;

  function submit(event: FormEvent) {
    event.preventDefault();
    save.mutate(toSettings(draft));
  }

  return (
    <div className="space-y-6">
      {status && !status.ok && (
        <SectionMessage appearance="warning">
          The assistant isn&apos;t working: its last check of the model provider failed.
          {status.message && <ErrorDetails details={status.message} />}
        </SectionMessage>
      )}
      <Panel>
        <form onSubmit={submit} noValidate aria-label="Assistant settings">
          <div className="divide-y divide-default">
            <Section
              title="Availability"
              description="The assistant replies only while this is on."
            >
              <Checkbox
                label="Enabled"
                isChecked={draft.enabled}
                onChange={(checked) => set("enabled", checked)}
              />
            </Section>
            <Section
              title="Connection"
              description="The model provider, and the credential it signs in with."
            >
              <Select
                label="Backend"
                options={data.backends.map((candidate) => ({
                  value: candidate.kind,
                  label: candidate.label,
                }))}
                value={draft.backend}
                onChange={(event) => switchTo(event.target.value, draft.auth_mode)}
                errorMessage={errors.backend}
              />
              <RadioGroup
                label="Auth"
                name="assistant-auth"
                isInline
                options={modes.map((mode) => ({ value: mode, label: AUTH_LABELS[mode] ?? mode }))}
                value={draft.auth_mode}
                onChange={(mode) => switchTo(draft.backend, mode)}
              />
              {errors.auth_mode && <ActionError error={new Error(errors.auth_mode)} />}
              {noLongerOffered && (
                <p className="flex flex-wrap items-center gap-2 text-body-sm text-subtle">
                  <Lozenge>No longer offered</Lozenge>
                  If you switch to another sign-in, you can&apos;t switch back to this one.
                </p>
              )}
              {draft.auth_mode === "bedrock" && draft.backend === "codex_cli" ? (
                <SectionMessage appearance="information">
                  Each reply uses a short-lived Bedrock token made from this host&apos;s AWS role.
                  Export credentials before starting the assistant process, as for agent runs on a
                  Bedrock runtime.
                </SectionMessage>
              ) : draft.backend !== data.settings.backend ? (
                <p className="text-body-sm text-subtlest">
                  Save the backend, then save its credential.
                </p>
              ) : draft.auth_mode !== data.settings.auth_mode ? (
                <p className="text-body-sm text-subtlest">
                  Save the auth mode, then save its credential.
                </p>
              ) : (
                <CredentialField
                  label={credentialLabel}
                  credential={{
                    set: savedCredential !== undefined,
                    updated_by: savedCredential?.updated_by ?? null,
                    updated_at: savedCredential?.updated_at ?? null,
                  }}
                  isPending={store.isPending || clear.isPending}
                  error={store.error ?? clear.error}
                  onStore={(value) =>
                    store.mutate({ backend: draft.backend, auth_mode: draft.auth_mode, value })
                  }
                  onRemove={() =>
                    clear.mutate({ backend: draft.backend, auth_mode: draft.auth_mode })
                  }
                />
              )}
              {draft.backend === "codex_cli" && draft.auth_mode === "bedrock" && (
                <TextField
                  label="Bedrock region"
                  isMonospaced
                  value={draft.bedrock_region}
                  onChange={(event) => set("bedrock_region", event.target.value)}
                  helperMessage="The AWS region of the host role's Bedrock token."
                  errorMessage={errors.bedrock_region}
                />
              )}
              {draft.auth_mode === "subscription" && (
                <SectionMessage appearance="information">
                  {draft.backend === "codex_cli" ? (
                    <>
                      Paste the contents of <code className="font-mono">~/.codex/auth.json</code>{" "}
                      from a machine signed in with <code className="font-mono">codex login</code>.
                    </>
                  ) : (
                    <>
                      The token comes from <code className="font-mono">claude setup-token</code>.
                    </>
                  )}{" "}
                  Every conversation in this organization counts against its owner's plan and
                  limits.
                </SectionMessage>
              )}
              <OtherCredentials
                credentials={data.credentials.filter((credential) => !isSaved(credential))}
                backends={data.backends}
              />
            </Section>
            <Section title="Model" description="Which model answers, and how hard it thinks.">
              <TextField
                label="Model"
                isMonospaced
                value={draft.model}
                onChange={(event) => set("model", event.target.value)}
                helperMessage={`A concrete model id, such as ${defaultModel(draft.backend, draft.auth_mode) ?? "claude-opus-5-5"}. An alias is refused.`}
                errorMessage={errors.model}
              />
              <RadioGroup
                label="Effort"
                name="assistant-effort"
                isInline
                options={EFFORTS}
                value={draft.effort}
                onChange={(effort) => set("effort", effort as Settings["effort"])}
              />
            </Section>
            <Section
              title="Limits"
              description="What one reply may spend, and what one person and the whole organization may spend in a day."
            >
              <div className="grid gap-4 sm:grid-cols-2">
                <TextField
                  label="Budget per reply (USD)"
                  inputMode="decimal"
                  value={draft.turn_budget_usd}
                  onChange={(event) => set("turn_budget_usd", event.target.value)}
                  helperMessage="The most one reply may spend."
                  errorMessage={errors.turn_budget_usd}
                />
                <TextField
                  label="Model steps per reply"
                  inputMode="numeric"
                  value={draft.max_steps}
                  onChange={(event) => set("max_steps", event.target.value)}
                  helperMessage="One step is one model response and the tools it calls."
                  errorMessage={errors.max_steps}
                />
                <TextField
                  label="Daily budget per person (USD)"
                  inputMode="decimal"
                  value={draft.daily_budget_usd}
                  onChange={(event) => set("daily_budget_usd", event.target.value)}
                  helperMessage="Checked when a message is sent."
                  errorMessage={errors.daily_budget_usd}
                />
                <TextField
                  label="Daily budget for the organization (USD)"
                  inputMode="decimal"
                  value={draft.organization_daily_budget_usd}
                  onChange={(event) => set("organization_daily_budget_usd", event.target.value)}
                  helperMessage="Leave empty for no limit. Checked when a message is sent."
                  errorMessage={errors.organization_daily_budget_usd}
                />
              </div>
              {draft.backend === "codex_cli" ? (
                <SectionMessage appearance="information">
                  Codex reports no cost, so these budgets don't limit it.
                </SectionMessage>
              ) : (
                draft.auth_mode === "subscription" && (
                  <SectionMessage appearance="information">
                    On a subscription, these budgets count each reply at API prices, as the SDK
                    estimates them. The plan's own limits are separate and can stop the assistant
                    first.
                  </SectionMessage>
                )
              )}
            </Section>
          </div>
          {/* The save is the form's, not the last section's: it sits in a footer of its own. */}
          <div className="flex flex-wrap items-center justify-end gap-3 border-t border-default px-4 py-3">
            {unplaced && <ActionError error={unplaced} />}
            {isDirty ? (
              <span className="text-body-sm text-subtle">Unsaved changes</span>
            ) : (
              save.isSuccess && (
                <span role="status" className="text-body-sm text-subtle">
                  Saved
                </span>
              )
            )}
            <Button type="submit" appearance="primary" isLoading={save.isPending}>
              Save
            </Button>
          </div>
        </form>
      </Panel>
      <TestPanel isDirty={isDirty} />
    </div>
  );
}

/** Credentials kept from an earlier backend or auth mode. The assistant reads only the saved
 *  pair's, so these can go. */
function OtherCredentials({
  credentials,
  backends,
}: {
  credentials: AssistantStoredCredential[];
  backends: AssistantBackend[];
}) {
  if (credentials.length === 0) return null;
  return (
    <div className="space-y-1">
      <span className="block text-body-sm font-semibold text-subtle">Other stored credentials</span>
      <ul className="space-y-2">
        {credentials.map((credential) => (
          <OtherCredential
            key={`${credential.backend}:${credential.auth_mode}`}
            credential={credential}
            label={`${backends.find((backend) => backend.kind === credential.backend)?.label ?? credential.backend} · ${AUTH_LABELS[credential.auth_mode] ?? credential.auth_mode}`}
          />
        ))}
      </ul>
    </div>
  );
}

function OtherCredential({
  credential,
  label,
}: {
  credential: AssistantStoredCredential;
  label: string;
}) {
  const clear = useClearAssistantCredential();
  return (
    <li className="space-y-1">
      <div className="flex flex-wrap items-center gap-2 text-body text-default">
        <span className="font-medium">{label}</span>
        <span className="text-subtle">
          Set by {credential.updated_by} <RelativeTime iso={credential.updated_at} />
        </span>
        <ConfirmPopup
          label="Remove"
          name={`Remove ${label}`}
          title="Remove the credential?"
          confirmLabel="Remove credential"
          consequence="The assistant doesn't use it now."
          appearance="danger"
          spacing="compact"
          resetKey={`${credential.backend}:${credential.auth_mode}`}
          isPending={clear.isPending}
          onConfirm={() =>
            clear.mutate({ backend: credential.backend, auth_mode: credential.auth_mode })
          }
        />
      </div>
      <ActionError error={clear.error} />
    </li>
  );
}

/** One group of the form: what it is and why on the left, its fields on the right — kept to a
 *  readable width on a full-width page, so a field is never a line across the screen. */
function Section({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <section className="grid gap-x-8 gap-y-3 px-4 py-5 lg:grid-cols-[16rem_minmax(0,1fr)]">
      <div className="space-y-1">
        <h3 className="text-heading-xxs text-default">{title}</h3>
        <p className="text-body-sm text-subtle">{description}</p>
      </div>
      <div className="max-w-2xl space-y-5">{children}</div>
    </section>
  );
}

function seconds(ms: number): string {
  return ms < 60_000 ? `${(ms / 1000).toFixed(1)}s` : formatElapsed(ms / 1000);
}

function TestPanel({ isDirty }: { isDirty: boolean }) {
  const { state, run } = useConnectionTest();
  return (
    <Panel
      title="Test"
      subtitle="Sends one short message with the saved settings and shows the reply."
      actions={
        <Button
          isLoading={state.phase === "running"}
          disabledReason={isDirty ? "Save your changes first." : undefined}
          onClick={run}
        >
          Test
        </Button>
      }
    >
      {(state.phase === "failed" || state.phase === "finished") && (
        <div className="space-y-2 p-4" aria-live="polite">
          {state.phase === "failed" && <ActionError error={state.error} />}
          {state.phase === "finished" && (
            <>
              {state.failure ? (
                <ActionError error={new Error(state.failure)} />
              ) : (
                <p className="text-body text-default">
                  <span className="text-subtle">Reply </span>
                  {state.reply || "(empty)"}
                </p>
              )}
              <p className="text-body-sm text-subtle">
                Answered in {seconds(state.elapsedMs)} · Cost{" "}
                {formatCost(state.costUsd, state.isEstimate)}
              </p>
            </>
          )}
        </div>
      )}
    </Panel>
  );
}
