/**
 * What a runtime needs before it can run, one row each: the model provider's key, token or
 * subscription, and on an E2B runtime its E2B team — the team's key and the template built in
 * it. Every row reads the same way: what it is, whether it is set, and the one way to set it.
 * A missing row opens its form; a set one keeps it behind Replace.
 *
 * Nothing here can show a credential. `Runtime.credentials` is names and clocks only, the key
 * field is uncontrolled so no value of it is ever in the markup, both it and the paste textarea
 * are cleared on the write that succeeds, and the device code is the only value the page ever
 * displays — it is not a credential, and it expires in fifteen minutes. The template is not a
 * credential, which is why it is the one value a row shows; it is picked from the team's own
 * builds, listed with the key the runtime already holds. Test credential shows only the API's
 * fixed sentences, never a vendor's own answer.
 */

import { useQueryClient } from "@tanstack/react-query";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../../api/client";
import { useOrganizationDetail } from "../../api/organizationQueries";
import {
  keys,
  useDeleteRuntimeCredential,
  usePutRuntimeCredential,
  useRuntimeLoginStatus,
  useRuntimeOptions,
  useRuntimeTemplates,
  useStartRuntimeLogin,
  useTestRuntimeCredential,
  useUpdateRuntime,
} from "../../api/queries";
import type {
  CredentialCheck,
  CredentialTest,
  Runtime,
  RuntimeCredential,
  RuntimeLogin,
  RuntimeLoginState,
  RuntimeTemplateBuild,
} from "../../api/types";
import { formatRelative, NO_VALUE } from "../../format";
import {
  Button,
  Code,
  ConfirmDialog,
  CopyButton,
  ErrorDetails,
  ErrorMessage,
  Form,
  FormFooter,
  Icon,
  Lozenge,
  RelativeTime,
  SectionMessage,
  Select,
  showFlag,
  TextArea,
  TextField,
  useDismissible,
} from "../ui";
import { managedKeyMeta, offerKey, runCount } from "./runtimeChoice";
import {
  buildVerdict,
  type CredentialForm as CredentialCopy,
  credentialFormFor,
  credentialNameFor,
  defaultBuild,
  E2B_CREDENTIAL,
  E2B_KEY_FORM,
  isolationOf,
  orderBuilds,
  RUNTIME_TYPE_LABELS,
  requiresStoredCredential,
  SUBSCRIPTION_CREDENTIAL,
  smokeKeyOf,
  templateOf,
  typeAvailable,
} from "./runtimes";

/** What an own-key runtime's group offers: a move to the Factorial-managed key, when the
 *  deployment covers its offer and its organization may use the key. */
function useManagedOffer(runtime: Runtime): { covered: boolean; limit: number | null } {
  const options = useRuntimeOptions();
  const limit = useOrganizationDetail().data?.managed_sandbox_limit ?? null;
  const covered =
    options.data?.managed_sandbox.includes(offerKey(runtime.type, "e2b")) === true &&
    limit !== null &&
    limit > 0;
  return { covered, limit };
}

export function Credentials({ runtime }: { runtime: Runtime }) {
  const e2b = isolationOf(runtime.details) === "e2b";
  const managed = e2b && runtime.sandbox_key === "managed";
  const offer = useManagedOffer(runtime);
  const read = new Set([
    credentialNameFor(runtime.type),
    ...(e2b && !managed ? [E2B_CREDENTIAL] : []),
  ]);
  const unread = runtime.credentials.filter((credential) => !read.has(credential.name));
  const copy = credentialFormFor(runtime.type);

  return (
    <div className="space-y-5 p-4">
      <Group title="Model provider">
        {!typeAvailable(runtime.type) ? (
          <li className="px-3 py-3">
            <SectionMessage appearance="information" title="Not available yet">
              {`${RUNTIME_TYPE_LABELS[runtime.type]} sign-in isn't available yet.`}
            </SectionMessage>
          </li>
        ) : !requiresStoredCredential(runtime.type) ? (
          <HostRoleRow />
        ) : copy ? (
          <SecretRow runtime={runtime} name={credentialNameFor(runtime.type)} copy={copy} />
        ) : (
          <SubscriptionRow runtime={runtime} />
        )}
      </Group>

      {e2b && (
        <Group
          title="Cloud sandbox"
          note={
            managed
              ? "Sandboxes run in Factorial's sandbox account, from a template Factorial provides."
              : "Sandboxes run in the team this key belongs to, from a template built in that team."
          }
        >
          {managed ? (
            <>
              <ManagedKeyRow runtime={runtime} limit={offer.limit} />
              <ManagedTemplateRow runtime={runtime} />
            </>
          ) : (
            <>
              <SecretRow
                runtime={runtime}
                name={E2B_CREDENTIAL}
                copy={E2B_KEY_FORM}
                id="field-credential-e2b"
                extra={
                  offer.covered &&
                  offer.limit !== null && <UseManagedKey runtime={runtime} limit={offer.limit} />
                }
              />
              <TemplateRow runtime={runtime} />
            </>
          )}
        </Group>
      )}

      <Group title="Check">
        {/* Keyed on the stored clocks: a result speaks for the credentials it was sent, so a
            replaced or removed one starts the row afresh. */}
        <CheckRow
          key={[...read].map((name) => stored(runtime, name)?.updated_at ?? "").join(" ")}
          runtime={runtime}
          e2b={e2b}
        />
      </Group>

      {unread.length > 0 && (
        <Group
          title="Unused credentials"
          note="Saved for a sign-in or sandbox this runtime no longer uses. You can remove them."
        >
          {unread.map((credential) => (
            <Row
              key={credential.name}
              title={<Code>{credential.name}</Code>}
              meta={clocks(credential)}
              actions={<RemoveCredential runtimeId={runtime.id} name={credential.name} />}
            />
          ))}
        </Group>
      )}
    </div>
  );
}

/** A heading and the rows it owns, boxed together so a row can only be read as part of it. */
function Group({ title, note, children }: { title: string; note?: string; children: ReactNode }) {
  return (
    <section className="space-y-2">
      <div>
        <h3 className="text-body-sm font-semibold text-subtle">{title}</h3>
        {note && <p className="text-body-sm text-subtlest">{note}</p>}
      </div>
      <ul className="divide-y divide-default rounded-md border border-default">{children}</ul>
    </section>
  );
}

/** The one shape every row takes: what it is and whether it is set, the facts under it, its
 *  actions on the right, and its form (when open) below. */
function Row({
  title,
  status,
  meta,
  actions,
  children,
}: {
  title: ReactNode;
  status?: ReactNode;
  meta: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <li className="space-y-3 px-3 py-3">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="min-w-0 space-y-0.5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-body font-medium text-default">{title}</span>
            {status}
          </div>
          <p className="text-body-sm text-subtle">{meta}</p>
        </div>
        {actions && <div className="flex shrink-0 items-center gap-1">{actions}</div>}
      </div>
      {children}
    </li>
  );
}

function HostRoleRow() {
  return (
    <Row
      title="Host AWS role"
      status={<Lozenge appearance="success">Connected</Lozenge>}
      meta="No key to store. Each run gets a short-lived Bedrock token from the host's AWS role."
    />
  );
}

/** Proves the stored credential now, and says when a run last found it refused. A codex
 *  subscription is only read for its expiry: the API will not refresh a login to test it. */
function CheckRow({ runtime, e2b }: { runtime: Runtime; e2b: boolean }) {
  const test = useTestRuntimeCredential(runtime.id);
  const failure = runtime.last_auth_failure;
  const how =
    runtime.type === "codex_subscription"
      ? "A local check. It reads when the stored sign-in expires and doesn't contact OpenAI."
      : runtime.type === "codex_bedrock"
        ? "Gets a short-lived Bedrock token from the host's AWS role and calls the configured model."
        : "One authenticated call to the model provider, which spends no tokens.";
  return (
    <Row
      title="Credential check"
      meta={e2b ? `${how} The sandbox key is checked beside it.` : how}
      actions={
        <Button spacing="compact" isLoading={test.isPending} onClick={() => test.mutate()}>
          Test credential
        </Button>
      }
    >
      {failure && (
        // The API reports only a run whose failure carries the provider's own refusal
        // (`_last_auth_failures`), so the plain line is true of every reason shown here.
        <div className="text-body-sm text-subtle">
          <p>
            Last sign-in failure: the provider rejected the credential (
            <Link to={`/tickets/${failure.ticket_id}`} className="underline underline-offset-2">
              {failure.ticket}
            </Link>
            , <RelativeTime iso={failure.at} />
            ).
          </p>
          <ErrorDetails details={failure.reason} />
        </div>
      )}
      <ErrorMessage error={test.error} />
      {test.data && <TestResult result={test.data} />}
    </Row>
  );
}

function TestResult({ result }: { result: CredentialTest }) {
  return (
    <div className="space-y-2">
      <Outcome title={result.local ? "Local check" : "Model provider"} check={result} />
      {result.e2b && <Outcome title="Cloud sandbox" check={result.e2b} />}
    </div>
  );
}

export function Outcome({ title, check }: { title: string; check: CredentialCheck }) {
  return (
    <SectionMessage appearance={check.ok ? "success" : "error"} title={title}>
      {check.detail}
    </SectionMessage>
  );
}

function SetLozenge({ set }: { set: boolean }) {
  return set ? (
    <Lozenge appearance="success">Stored</Lozenge>
  ) : (
    <Lozenge appearance="warning">Needed to run</Lozenge>
  );
}

function clocks(credential: RuntimeCredential): string {
  const replaced = credential.updated_at !== credential.created_at;
  return `stored ${formatRelative(credential.created_at)}${
    replaced ? ` · updated ${formatRelative(credential.updated_at)}` : ""
  }`;
}

function stored(runtime: Runtime, name: string): RuntimeCredential | undefined {
  return runtime.credentials.find((credential) => credential.name === name);
}

/** A key or a token: one password field, open while nothing is stored and behind Replace once
 *  something is. A replace never shows what it replaces. */
function SecretRow({
  runtime,
  name,
  copy,
  id = "field-credential",
  extra,
}: {
  runtime: Runtime;
  name: string;
  copy: CredentialCopy;
  id?: string;
  /** One more action beside the row's own. */
  extra?: ReactNode;
}) {
  const held = stored(runtime, name);
  const [replacing, setReplacing] = useState(false);
  return (
    <Row
      title={copy.label}
      status={<SetLozenge set={held !== undefined} />}
      meta={
        <>
          <Code>{name}</Code> · {held ? clocks(held) : copy.hint}
        </>
      }
      actions={
        (held && !replacing) || extra ? (
          <>
            {held && !replacing && (
              <>
                <Button spacing="compact" onClick={() => setReplacing(true)}>
                  Replace
                </Button>
                <RemoveCredential runtimeId={runtime.id} name={name} />
              </>
            )}
            {extra}
          </>
        ) : undefined
      }
    >
      {(!held || replacing) && (
        <SecretForm
          runtime={runtime}
          name={name}
          copy={copy}
          id={id}
          onCancel={held ? () => setReplacing(false) : undefined}
          onSaved={() => setReplacing(false)}
        />
      )}
    </Row>
  );
}

/**
 * The input is uncontrolled and read off the form on submit, so the value the operator types
 * is never a prop, never an attribute, never in the markup; the form is reset on the write that
 * succeeds and left alone on one that fails, as `PasteBundle` is.
 */
function SecretForm({
  runtime,
  name,
  copy,
  id,
  onCancel,
  onSaved,
}: {
  runtime: Runtime;
  name: string;
  copy: CredentialCopy;
  id: string;
  onCancel?: () => void;
  onSaved: () => void;
}) {
  const put = usePutRuntimeCredential(runtime.id);
  const [filled, setFilled] = useState(false);
  return (
    <Form
      validate={() => filled}
      onSubmit={(form) => {
        const value = String(new FormData(form).get("credential") ?? "");
        put.mutate(
          { name, value },
          {
            onSuccess: () => {
              form.reset();
              setFilled(false);
              onSaved();
              showFlag({
                appearance: "success",
                title: `Saved ${copy.label}`,
                description: "Stored encrypted. You can't view it again.",
              });
            },
          },
        );
      }}
      className="space-y-2"
    >
      {({ attempted }) => (
        <>
          <TextField
            id={id}
            name="credential"
            label={onCancel ? `New ${copy.label}` : copy.label}
            labelHidden
            type="password"
            autoComplete="off"
            spellCheck={false}
            placeholder={copy.placeholder}
            onChange={(event) => setFilled(event.target.value !== "")}
            errorMessage={attempted && !filled ? `Paste the ${copy.noun} to save it.` : undefined}
          />
          <ErrorMessage error={put.error} />
          <FormFooter>
            {onCancel && <Button onClick={onCancel}>Cancel</Button>}
            <Button type="submit" appearance="primary" isLoading={put.isPending}>
              {`Save ${copy.noun}`}
            </Button>
          </FormFooter>
        </>
      )}
    </Form>
  );
}

/** A runtime on the Factorial-managed key holds no key. Storing its own is the move off it: the
 *  API flips the runtime in the same request, so the row reuses the key form. */
function ManagedKeyRow({ runtime, limit }: { runtime: Runtime; limit: number | null }) {
  const [storing, setStoring] = useState(false);
  return (
    <Row
      title="Sandbox key"
      status={<Lozenge appearance="neutral">Managed</Lozenge>}
      meta={managedKeyMeta(limit)}
      actions={
        !storing && (
          <Button spacing="compact" onClick={() => setStoring(true)}>
            Use your own key
          </Button>
        )
      }
    >
      {runtime.sandbox_gap && (
        <SectionMessage appearance="warning" title="No sandbox can start">
          {runtime.sandbox_gap}
        </SectionMessage>
      )}
      {storing && (
        <SecretForm
          runtime={runtime}
          name={E2B_CREDENTIAL}
          copy={E2B_KEY_FORM}
          id="field-credential-e2b"
          onCancel={() => setStoring(false)}
          onSaved={() => setStoring(false)}
        />
      )}
    </Row>
  );
}

function ManagedTemplateRow({ runtime }: { runtime: Runtime }) {
  return (
    <Row
      title="Sandbox template"
      meta={runtime.template ? <Code>{runtime.template}</Code> : NO_VALUE}
    />
  );
}

/** A PATCH replaces `details`, and the API refuses a template on this move, so every other key
 *  the runtime holds is sent back. */
function managedDetails(details: Record<string, unknown>): Record<string, unknown> {
  const { template: _template, ...kept } = details;
  return { ...kept, isolation: "e2b", sandbox_key: "managed" };
}

function UseManagedKey({ runtime, limit }: { runtime: Runtime; limit: number }) {
  const update = useUpdateRuntime(runtime.id);
  const [confirming, setConfirming] = useState(false);
  return (
    <>
      <Button spacing="compact" onClick={() => setConfirming(true)}>
        Use the Factorial-managed key
      </Button>
      {confirming && (
        <ConfirmDialog
          title="Use the Factorial-managed key?"
          consequence={`The runtime's own sandbox key and its template are removed. Sandboxes then run in Factorial's sandbox account, up to ${runCount(limit)} at a time for this organization.`}
          confirmLabel="Use the managed key"
          isPending={update.isPending}
          error={update.error}
          onCancel={() => {
            update.reset();
            setConfirming(false);
          }}
          onConfirm={() =>
            update.mutate(
              { details: managedDetails(runtime.details) },
              { onSuccess: () => setConfirming(false) },
            )
          }
        />
      )}
    </>
  );
}

function RemoveCredential({ runtimeId, name }: { runtimeId: number; name: string }) {
  const remove = useDeleteRuntimeCredential(runtimeId);
  const [confirming, setConfirming] = useState(false);
  return (
    <>
      <Button spacing="compact" onClick={() => setConfirming(true)}>
        Remove
      </Button>
      {confirming && (
        <ConfirmDialog
          title={`Remove ${name}?`}
          consequence="You can't undo this. Runs on this runtime can't start until you save it again."
          confirmLabel="Remove credential"
          isPending={remove.isPending}
          error={remove.error}
          onCancel={() => {
            remove.reset();
            setConfirming(false);
          }}
          onConfirm={() =>
            remove.mutate(name, {
              onSuccess: () => {
                setConfirming(false);
                showFlag({ appearance: "success", title: `Removed ${name}` });
              },
            })
          }
        />
      )}
    </>
  );
}

/** The build an E2B runtime boots. Chosen from the ready builds of the team the runtime's key
 *  names, and only once it holds one: a template is private to the team that built it. Not a
 *  secret, so the row shows it. */
function TemplateRow({ runtime }: { runtime: Runtime }) {
  const current = templateOf(runtime.details);
  const keyed = runtime.credential_names.includes(E2B_CREDENTIAL);
  const [editing, setEditing] = useState(false);
  const choosing = keyed && (current === "" || editing);

  return (
    <Row
      title="Sandbox template"
      status={<SetLozenge set={current !== ""} />}
      meta={
        current ? (
          <Code>{current}</Code>
        ) : keyed ? (
          "Choose one of this team's builds."
        ) : (
          "Chosen from the team's builds once its sandbox API key is stored."
        )
      }
      actions={
        keyed &&
        current &&
        !editing && (
          <Button spacing="compact" onClick={() => setEditing(true)}>
            Change
          </Button>
        )
      }
    >
      {choosing ? (
        <TemplatePicker
          runtime={runtime}
          current={current}
          onCancel={current ? () => setEditing(false) : undefined}
          onSaved={() => setEditing(false)}
        />
      ) : (
        keyed && current !== "" && <BuildVerdict runtime={runtime} />
      )}
    </Row>
  );
}

/** The worker refuses a build that has not passed the session smoke (decision 25), so the row
 *  says so before a ticket blocks on it. The verdict comes from the team's build list, which
 *  the picker reads too. */
function BuildVerdict({ runtime }: { runtime: Runtime }) {
  const pin = templateOf(runtime.details);
  const builds = useRuntimeTemplates(runtime.id, { enabled: true });
  const verdict = buildVerdict(builds.data, pin);
  if (verdict !== "unverified" && verdict !== "unlisted") return null;
  return (
    <p className="flex items-start gap-1 text-body-sm text-warning">
      <Icon glyph="status-warning" size="small" className="mt-0.5" />
      {verdict === "unlisted" ? (
        <span>
          This template isn&apos;t among the team&apos;s builds, so no run can start on it. Choose
          another build.
        </span>
      ) : (
        <span>
          This template hasn&apos;t passed the interactive test, so no run can start on it. Choose a
          build marked verified, or test it:{" "}
          <Code>
            E2B_API_KEY=YOUR_E2B_KEY {smokeKeyOf(runtime.type)}=YOUR_MODEL_KEY uv run python -m
            orchestrator.e2b_template --interactive-smoke {pin}
          </Code>{" "}
          or, from the image:{" "}
          <Code>
            docker exec -it -e E2B_API_KEY -e {smokeKeyOf(runtime.type)} factorial python -m
            orchestrator.e2b_template --interactive-smoke {pin}
          </Code>
        </span>
      )}
    </p>
  );
}

export function buildLabel(build: RuntimeTemplateBuild, current: string): string {
  const size = `${build.cpu_count} vCPU, ${build.memory_mb / 1024} GiB`;
  const built = `built ${formatRelative(build.created_at)}`;
  const verdict = build.verified ? " · verified" : "";
  const mark = build.pin === current ? " (current)" : "";
  const harness = build.matches_harness ? "" : " · other CLI";
  return `${build.template} · ${build.build_id.slice(0, 8)} · ${built} · ${size}${verdict}${mark}${harness}`;
}

function TemplatePicker({
  runtime,
  current,
  onCancel,
  onSaved,
}: {
  runtime: Runtime;
  current: string;
  onCancel?: () => void;
  onSaved: () => void;
}) {
  const builds = useRuntimeTemplates(runtime.id, { enabled: true });
  const update = useUpdateRuntime(runtime.id);
  const ordered = orderBuilds(builds.data ?? []);
  const [picked, setPicked] = useState<string | null>(null);
  const choice = picked ?? defaultBuild(ordered, current)?.pin ?? current;

  if (builds.isPending) {
    return <p className="text-body-sm text-subtle">Loading this team's builds…</p>;
  }
  if (builds.error) {
    return <ErrorMessage error={builds.error} onRetry={() => builds.refetch()} />;
  }
  if (ordered.length === 0) {
    return (
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-md bg-neutral p-3">
        <p className="text-body-sm text-subtle">
          This team has no ready builds. Build them with its key:{" "}
          <Code>E2B_API_KEY=YOUR_E2B_KEY make e2b-template</Code> or, from the image:{" "}
          <Code>
            docker exec -it -e E2B_API_KEY -e ANTHROPIC_API_KEY factorial python -m
            orchestrator.e2b_template claude
          </Code>{" "}
          (codex: <Code>-e OPENAI_API_KEY</Code> and <Code>codex</Code>)
        </p>
        <Button spacing="compact" isLoading={builds.isFetching} onClick={() => builds.refetch()}>
          Look again
        </Button>
      </div>
    );
  }
  return (
    <form
      className="space-y-2"
      onSubmit={(event) => {
        event.preventDefault();
        update.mutate(
          { details: { ...runtime.details, template: choice } },
          {
            onSuccess: () => {
              onSaved();
              showFlag({ appearance: "success", title: "Saved the template" });
            },
          },
        );
      }}
    >
      <Select
        id="field-template"
        label="Template build"
        labelHidden
        width="full"
        value={choice}
        onChange={(event) => setPicked(event.target.value)}
        options={[
          // A current pin the team no longer lists, or none at all, has no option of its own.
          ...(ordered.some((build) => build.pin === choice)
            ? []
            : [{ value: choice, label: "Choose a build" }]),
          ...ordered.map((build) => ({
            value: build.pin,
            label: buildLabel(build, current),
          })),
        ]}
      />
      <ErrorMessage error={update.error} />
      <FormFooter>
        {onCancel && <Button onClick={onCancel}>Cancel</Button>}
        <Button
          type="submit"
          appearance="primary"
          isLoading={update.isPending}
          isDisabled={choice === current}
        >
          Save template
        </Button>
      </FormFooter>
    </form>
  );
}

/**
 * The codex subscription: the device flow first — one click starts `codex login --device-auth`
 * on the host, the URL and the one-time code come back, and the page polls until the login
 * settles — and pasting `auth.json` behind its own button, for a host with no browser at hand.
 * Completion is the API storing the bundle, so the runtime list is invalidated then.
 */
function SubscriptionRow({ runtime }: { runtime: Runtime }) {
  const start = useStartRuntimeLogin(runtime.id);
  const [login, setLogin] = useState<RuntimeLogin | null>(null);
  const [pasting, setPasting] = useState(false);
  const status = useRuntimeLoginStatus(runtime.id, login?.session_id ?? null);
  const client = useQueryClient();
  // A lost session (the API restarted) must release the button, not pin it on "pending".
  const state = status.error ? "failed" : (status.data?.status ?? (login ? "pending" : null));
  const held = stored(runtime, SUBSCRIPTION_CREDENTIAL);

  useEffect(() => {
    if (state === "complete") client.invalidateQueries({ queryKey: keys.runtimes });
  }, [state, client]);

  // The create page sends `?connect=chatgpt` after making a Codex runtime that signs in with
  // ChatGPT. The ref outlives StrictMode's second effect, so one visit starts one login. The
  // call skips the mutation: StrictMode's remount drops a per-call `mutate` callback and leaves
  // the observer pending, so the code never showed and the button spun for good.
  const [params, setParams] = useSearchParams();
  const autoStarted = useRef(false);
  const [autoPending, setAutoPending] = useState(false);
  const [autoError, setAutoError] = useState<unknown>(null);
  const connect = params.get("connect") === "chatgpt";
  useEffect(() => {
    if (!connect || autoStarted.current) return;
    autoStarted.current = true;
    if (!held) {
      setAutoPending(true);
      api
        .startRuntimeLogin(runtime.id)
        .then(setLogin, setAutoError)
        .finally(() => setAutoPending(false));
    }
    setParams(
      (next) => {
        next.delete("connect");
        return next;
      },
      { replace: true },
    );
  }, [connect, held, runtime.id, setParams]);

  return (
    <Row
      title="ChatGPT sign-in"
      status={<SetLozenge set={held !== undefined} />}
      meta={
        <>
          <Code>{SUBSCRIPTION_CREDENTIAL}</Code> ·{" "}
          {held
            ? clocks(held)
            : "Sign in with your ChatGPT subscription. Runs use your plan's limits."}
        </>
      }
      actions={
        <>
          <Button
            appearance={held ? "default" : "primary"}
            spacing="compact"
            isLoading={start.isPending || autoPending || state === "pending"}
            onClick={() => {
              setAutoError(null);
              start.mutate(undefined, { onSuccess: setLogin });
            }}
          >
            {held ? "Reconnect subscription" : "Connect subscription"}
          </Button>
          {!pasting && (
            <Button spacing="compact" onClick={() => setPasting(true)}>
              Paste auth.json
            </Button>
          )}
          {held && <RemoveCredential runtimeId={runtime.id} name={SUBSCRIPTION_CREDENTIAL} />}
        </>
      }
    >
      <ErrorMessage error={start.error ?? autoError ?? status.error} />
      {login && state !== null && (
        <DeviceCode
          login={login}
          state={state}
          detail={
            status.data?.detail ??
            (status.error ? "The sign-in session was lost. Connect again." : null)
          }
        />
      )}
      {pasting && <PasteBundle runtime={runtime} onDone={() => setPasting(false)} />}
    </Row>
  );
}

function DeviceCode({
  login,
  state,
  detail,
}: {
  login: RuntimeLogin;
  state: RuntimeLoginState;
  detail: string | null;
}) {
  // Each Connect starts a new login, so the next one's Connected shows again.
  const [connected, dismiss] = useDismissible(state === "complete" ? login : null);
  if (state === "complete") {
    return connected ? (
      <SectionMessage appearance="success" title="Connected" onDismiss={dismiss} />
    ) : null;
  }
  if (state === "failed") {
    return (
      <SectionMessage appearance="error" title="Sign-in failed" isAlert>
        {detail}
      </SectionMessage>
    );
  }
  if (state === "expired") {
    return (
      <SectionMessage appearance="warning" title="The code expired">
        The sign-in didn't finish in time. Connect again for a new code.
      </SectionMessage>
    );
  }
  return (
    <div className="space-y-2 rounded-md bg-neutral p-4">
      <p className="text-body text-default">
        Open{" "}
        <a
          href={login.verification_url}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 underline underline-offset-2"
        >
          {login.verification_url}
          <Icon glyph="link-external" size="small" />
        </a>{" "}
        and enter this code:
      </p>
      <div className="flex items-center gap-3">
        <output className="font-mono text-metric-md tracking-[0.2em] text-default">
          {login.user_code}
        </output>
        <CopyButton value={login.user_code} label="Copy code" />
      </div>
      <p className="text-body-sm text-subtle">
        The code expires {formatRelative(login.expires_at)}. This page updates once you finish
        signing in.
      </p>
    </div>
  );
}

/**
 * `~/.codex/auth.json` from a machine that has signed in. Cleared only on success — a rejected
 * write leaves the text in the box, or the operator fetches it again — and never written back
 * once stored.
 */
function PasteBundle({ runtime, onDone }: { runtime: Runtime; onDone: () => void }) {
  const put = usePutRuntimeCredential(runtime.id);
  const [value, setValue] = useState("");

  return (
    <Form
      validate={() => value.trim() !== ""}
      onSubmit={() =>
        put.mutate(
          { name: SUBSCRIPTION_CREDENTIAL, value },
          {
            onSuccess: () => {
              setValue("");
              onDone();
              showFlag({
                appearance: "success",
                title: "Saved the ChatGPT sign-in",
                description: "Stored encrypted. You can't view it again.",
              });
            },
          },
        )
      }
      className="space-y-2"
    >
      {({ attempted }) => (
        <>
          <TextArea
            id="field-auth-json"
            label="auth.json"
            value={value}
            onChange={(event) => setValue(event.target.value)}
            minimumRows={4}
            spellCheck={false}
            autoComplete="off"
            isMonospaced
            placeholder="The contents of ~/.codex/auth.json from a signed-in machine"
            errorMessage={
              attempted && value.trim() === "" ? "Paste the file's contents first." : undefined
            }
          />
          <ErrorMessage error={put.error} />
          <FormFooter>
            <Button onClick={onDone}>Cancel</Button>
            <Button type="submit" appearance="primary" isLoading={put.isPending}>
              Save auth.json
            </Button>
          </FormFooter>
        </>
      )}
    </Form>
  );
}
