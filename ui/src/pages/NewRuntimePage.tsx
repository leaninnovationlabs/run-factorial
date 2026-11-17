/**
 * Create a runtime: a CLI, how it signs in and where it runs, each chosen on cards with the keys
 * that choice asks for under them, then its name. Its own page, like `/projects/new`: the slug it fixes is the one thing a repo
 * will depend on.
 *
 * The cards are what this deployment offers (`GET /api/runtime-options`), and the API refuses
 * the rest. Until that list is read, the page shows no cards. One request stores the runtime and
 * its keys, all or nothing. The keys are read off the form only inside the Test and Create
 * handlers and go straight through the API client, never through React state or a query
 * mutation. Each key has its own Test: the model key's sends only the model credential, the
 * sandbox key's only the sandbox key, and each outcome stands until its own fields or the cards
 * it depends on change. When the organization may use the deployment's managed sandbox key, the
 * Sandbox step offers it as its own card beside the own E2B one: it opens nothing and stores no
 * key or template. The page waits for the organization as it waits for the offered list, so the
 * card never appears under the person; an organization it cannot read gets no managed card. A
 * Codex runtime that signs in with ChatGPT opens its own page with `?connect=chatgpt`, which
 * starts the device login there.
 *
 * The name and the slug follow the cards until the operator edits one; then the slug follows the
 * name. `?slug=` presets the slug, and the cards when it has the shape of a suggested one.
 */

import { useQueryClient } from "@tanstack/react-query";
import { type FormEvent, type ReactNode, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ApiError, api } from "../api/client";
import { useOrganizationDetail } from "../api/organizationQueries";
import { keys, useRuntimeOptions } from "../api/queries";
import type { Runtime, RuntimeCheckOut } from "../api/types";
import { CreateAuthFields } from "../components/runtimes/CreateAuthFields";
import { CreateSandboxFields } from "../components/runtimes/CreateSandboxFields";
import { CreateSummary } from "../components/runtimes/CreateSummary";
import {
  type Auth,
  authCards,
  type Choice,
  CLI_CARDS,
  type Cli,
  type CodexSignIn,
  cardOptions,
  cardStates,
  choiceForSandboxCard,
  choiceFromSlug,
  choiceLabels,
  choiceProblem,
  defaultChoice,
  deferredLogin,
  detailsFor,
  fieldsFor,
  ISOLATION_FOR,
  managedCovers,
  reconcile,
  type SandboxCard,
  type SandboxKey,
  sandboxCardOf,
  sandboxCardOptions,
  suggestName,
  suggestSlugFor,
  typeFor,
  usesManagedKey,
} from "../components/runtimes/runtimeChoice";
import {
  defaultBuild,
  E2B_CREDENTIAL,
  orderBuilds,
  SLUG_RE,
  suggestSlug,
} from "../components/runtimes/runtimes";
import {
  ErrorDetails,
  ErrorMessage,
  PageHeader,
  Panel,
  RadioGroup,
  SectionMessage,
  Spinner,
  TextField,
  usePageTitle,
} from "../components/ui";

const FALLBACK: Choice = { cli: "claude", auth: "api_key", sandbox: "host", sandboxKey: "own" };
const NONE: ReadonlySet<string> = new Set();

/** A Test outcome for one version of its fields, with the template picked from its builds. */
type Check = {
  version: number;
  result: RuntimeCheckOut | null;
  error: unknown;
  template: string | null;
};

function Step({
  n,
  title,
  note,
  children,
}: {
  n: number;
  title: string;
  note?: string;
  children: ReactNode;
}) {
  return (
    <Panel title={`${n}. ${title}`} subtitle={note}>
      <div className="space-y-4 p-4">{children}</div>
    </Panel>
  );
}

/** A refusal names the problem; no answer at all, or a 5xx, may still have committed. */
function unconfirmed(error: unknown): boolean {
  return !(error instanceof ApiError) || error.status === 0 || error.status >= 500;
}

export function NewRuntimePage() {
  usePageTitle(["Create runtime"]);
  const navigate = useNavigate();
  const client = useQueryClient();
  const options = useRuntimeOptions();
  const offered = useMemo(
    () => (options.data ? new Set(options.data.offered) : null),
    [options.data],
  );
  const organization = useOrganizationDetail();
  const limit = organization.data?.managed_sandbox_limit ?? null;
  // An organization with no limit above 0 cannot use the managed key, so nothing is covered.
  const managed = useMemo(
    () => (limit !== null && limit > 0 ? new Set(options.data?.managed_sandbox) : NONE),
    [options.data, limit],
  );
  const form = useRef<HTMLFormElement>(null);
  // A second press before the first re-render must not send a second create.
  const inFlight = useRef(false);

  const [params] = useSearchParams();
  const presetSlug = useMemo(() => {
    const wanted = params.get("slug");
    return wanted !== null && SLUG_RE.test(wanted) ? wanted : null;
  }, [params]);
  const [picked, setPicked] = useState<Choice | null>(null);
  // Only a pick of the managed or the own card sets this: a forced own (uncovered) is never the
  // person's choice.
  const [keyPicked, setKeyPicked] = useState<SandboxKey | null>(null);
  const [codexSignIn, setCodexSignIn] = useState<CodexSignIn>("chatgpt");
  const [nameEdit, setNameEdit] = useState<string | null>(null);
  const [slugEdit, setSlugEdit] = useState<string | null>(presetSlug);
  const [authVersion, setAuthVersion] = useState(0);
  const [sandboxVersion, setSandboxVersion] = useState(0);
  const [authCheck, setAuthCheck] = useState<Check | null>(null);
  const [sandboxCheck, setSandboxCheck] = useState<Check | null>(null);
  // One flag per Test button: the two can be in flight at once.
  const [checking, setChecking] = useState<{ auth: boolean; sandbox: boolean }>({
    auth: false,
    sandbox: false,
  });
  const [attempted, setAttempted] = useState(false);
  const [missing, setMissing] = useState<ReadonlySet<string>>(new Set());
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [slugConflict, setSlugConflict] = useState<{ slug: string; message: string } | null>(null);

  const initial = {
    ...((offered && defaultChoice(offered, managed)) ?? FALLBACK),
    ...(presetSlug ? choiceFromSlug(presetSlug) : null),
  };
  const choice = reconcile(offered, managed, picked ?? initial, keyPicked);
  const states = cardStates(offered, choice);
  const type = typeFor(choice.cli, choice.auth);
  const isolation = ISOLATION_FOR[choice.sandbox];
  const target = type && isolation ? { type, isolation } : null;
  const fields = fieldsFor(choice, codexSignIn);
  const authCurrent = authCheck?.version === authVersion ? authCheck : null;
  const sandboxCurrent = sandboxCheck?.version === sandboxVersion ? sandboxCheck : null;
  const sandboxResult = sandboxCurrent?.result ?? null;
  // The one place a template is chosen: never on the managed key, whatever a test listed.
  const templates =
    choice.sandbox === "cloud" && !usesManagedKey(choice) && sandboxResult?.e2b?.ok
      ? orderBuilds(sandboxResult.templates)
      : null;
  const chosenTemplate = (templates && sandboxCurrent?.template) || null;
  const problem = choiceProblem(offered, choice, states);

  const name = nameEdit ?? suggestName(choice);
  const slug = slugEdit ?? (nameEdit === null ? suggestSlugFor(choice) : suggestSlug(name));
  const nameProblem = name.trim() === "" ? "Give the runtime a name." : null;
  const slugProblem = !SLUG_RE.test(slug)
    ? "Use up to 63 lowercase letters, digits and dashes. Start with a letter or digit."
    : null;
  const slugTaken = slugConflict?.slug === slug ? slugConflict.message : null;
  // A typed bad slug shows at once. An empty one, which an emptied name proposes, waits for
  // Create as the name's error does.
  const slugError =
    slugTaken ?? ((attempted || slug !== "") && slugProblem ? slugProblem : undefined);

  // An edit to one key outdates that key's test only: another key's passed test, and the
  // template list it opened, still stand.
  function authChanged() {
    setAuthVersion((version) => version + 1);
    setAuthCheck(null);
    setMissing(new Set());
  }

  function sandboxChanged() {
    setSandboxVersion((version) => version + 1);
    setSandboxCheck(null);
    setMissing(new Set());
  }

  // A different CLI changes which model the key is tried against and which build the template
  // list prefers, so both tests go. A different authentication changes only the model key. A
  // different sandbox changes what a model test means (a local check or the sandbox's) and
  // remounts or drops the sandbox key, so both go. So does any card that moves the sandbox
  // between the managed key and an own one: an authentication the managed key does not cover
  // forces own.
  function pick(next: Partial<Choice>) {
    const wanted = { ...choice, ...next };
    setPicked(wanted);
    authChanged();
    const key = reconcile(offered, managed, wanted, next.sandboxKey ?? keyPicked).sandboxKey;
    if (
      next.cli !== undefined ||
      next.sandbox !== undefined ||
      next.sandboxKey !== undefined ||
      key !== choice.sandboxKey
    ) {
      sandboxChanged();
    }
  }

  function readCredentials(element: HTMLFormElement): Record<string, string> {
    const data = new FormData(element);
    return Object.fromEntries(fields.map((each) => [each, String(data.get(each) ?? "")]));
  }

  async function runCheck(which: "auth" | "sandbox") {
    if (!form.current || target === null) return;
    const wanted = (name: string) => (which === "sandbox") === (name === E2B_CREDENTIAL);
    const credentials = Object.fromEntries(
      Object.entries(readCredentials(form.current)).filter(
        ([name, value]) => wanted(name) && value.trim() !== "",
      ),
    );
    const sandbox = which === "sandbox";
    const version = sandbox ? sandboxVersion : authVersion;
    const set = sandbox ? setSandboxCheck : setAuthCheck;
    setChecking((now) => ({ ...now, [which]: true }));
    set((previous) => previous && { ...previous, error: null });
    try {
      const checked = await api.checkRuntime({
        type: target.type,
        details: detailsFor(choice),
        credentials,
      });
      const template = defaultBuild(orderBuilds(checked.templates))?.pin ?? null;
      set({ version, result: checked, error: null, template });
    } catch (caught) {
      set({
        version,
        result: null,
        error: unconfirmed(caught) ? "Couldn't check the runtime. Try again." : caught,
        template: null,
      });
    } finally {
      setChecking((now) => ({ ...now, [which]: false }));
    }
  }

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const element = event.currentTarget;
    if (inFlight.current || problem !== null || target === null) return;
    const credentials = readCredentials(element);
    const empty = new Set(fields.filter((each) => credentials[each]?.trim() === ""));
    setAttempted(true);
    setMissing(empty);
    if (nameProblem || slugProblem || empty.size > 0) {
      requestAnimationFrame(() =>
        element.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus(),
      );
      return;
    }
    inFlight.current = true;
    setPending(true);
    setError(null);
    setSlugConflict(null);
    try {
      const row = await api.createRuntime({
        name: name.trim(),
        slug,
        type: target.type,
        details: {
          ...detailsFor(choice),
          ...(chosenTemplate ? { template: chosenTemplate } : {}),
        },
        ...(fields.length > 0 ? { credentials } : {}),
      });
      client.setQueryData<Runtime[]>(keys.runtimes, (rows) => rows && [...rows, row]);
      void client.invalidateQueries({ queryKey: keys.runtimes });
      const connect = deferredLogin(target.type, codexSignIn) ? "?connect=chatgpt" : "";
      navigate(`/runtimes/${row.slug}${connect}`);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409) {
        setSlugConflict({ slug, message: caught.message });
        requestAnimationFrame(() => document.getElementById("field-slug")?.focus());
      } else {
        setError(caught);
      }
    } finally {
      inFlight.current = false;
      setPending(false);
    }
  }

  // The organization decides whether the managed card exists; an unread offered list does not
  // wait for it to say so.
  if (options.isPending || (offered !== null && organization.isPending)) {
    return (
      <div className="space-y-6">
        <PageHeader title="Create runtime" />
        <Spinner label="Loading the available options" />
      </div>
    );
  }

  if (offered === null) {
    return (
      <div className="space-y-6">
        <PageHeader title="Create runtime" />
        <ErrorMessage
          error={options.error}
          title="Couldn't load the available options"
          onRetry={() => options.refetch()}
        />
      </div>
    );
  }

  const labels = choiceLabels(choice);
  return (
    <div className="space-y-6">
      <PageHeader title="Create runtime" />
      <ErrorMessage
        error={options.error}
        title="Couldn't load the available options"
        onRetry={() => options.refetch()}
      />
      <form
        ref={form}
        noValidate
        onSubmit={create}
        aria-label="Create runtime"
        className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_20rem]"
      >
        <div className="min-w-0 space-y-6">
          <Step n={1} title="CLI" note="The coding agent that does the work.">
            <RadioGroup
              appearance="tile"
              label="CLI"
              name="cli"
              value={choice.cli}
              onChange={(value) => pick({ cli: value as Cli })}
              options={cardOptions(CLI_CARDS, states.cli)}
            />
          </Step>
          <Step
            n={2}
            title="Authentication"
            note={`How ${labels.cli} signs in to its model provider.`}
          >
            <RadioGroup
              appearance="tile"
              label="Authentication"
              name="auth"
              value={choice.auth}
              onChange={(value) => pick({ auth: value as Auth })}
              options={cardOptions(authCards(choice.cli), states.auth)}
            />
            <CreateAuthFields
              choice={choice}
              codexSignIn={codexSignIn}
              onCodexSignIn={(value) => {
                setCodexSignIn(value);
                authChanged();
              }}
              onEdit={authChanged}
              missing={missing}
              test={{
                onTest: () => void runCheck("auth"),
                isPending: checking.auth,
                result: authCurrent?.result ?? null,
                error: authCurrent?.error ?? null,
              }}
            />
          </Step>
          <Step n={3} title="Sandbox" note="Where each run's code executes.">
            <RadioGroup
              appearance="tile"
              label="Sandbox"
              name="sandbox"
              value={sandboxCardOf(choice)}
              onChange={(value) => {
                const next = choiceForSandboxCard(value as SandboxCard);
                // A pick only where both cards could be chosen: under a disabled managed card
                // the own card is the one way to the cloud, not a choice against managed.
                if (next.sandboxKey && managedCovers(managed, choice)) {
                  setKeyPicked(next.sandboxKey);
                }
                pick(next);
              }}
              options={sandboxCardOptions(states.sandbox, managed, limit, choice)}
            />
            {sandboxCardOf(choice) === "own" && (
              <CreateSandboxFields
                onEdit={sandboxChanged}
                missing={missing}
                templates={templates}
                template={chosenTemplate}
                onTemplate={(pin) =>
                  setSandboxCheck((previous) => previous && { ...previous, template: pin })
                }
                test={{
                  onTest: () => void runCheck("sandbox"),
                  isPending: checking.sandbox,
                  result: sandboxResult,
                  error: sandboxCurrent?.error ?? null,
                }}
              />
            )}
          </Step>
          <Step n={4} title="Name">
            <TextField
              label="Name"
              isRequired
              value={name}
              onChange={(event) => setNameEdit(event.target.value)}
              helperMessage="Suggested from your choices. You can change it."
              errorMessage={attempted ? (nameProblem ?? undefined) : undefined}
            />
            <TextField
              id="field-slug"
              label="Slug"
              isRequired
              value={slug}
              onChange={(event) => setSlugEdit(event.target.value)}
              spellCheck={false}
              isMonospaced
              helperMessage="The value a process file gives as its runtime. You can't change it later."
              errorMessage={slugError}
            />
          </Step>
          {error !== null && unconfirmed(error) && (
            <SectionMessage
              appearance="error"
              title="Couldn't tell if the runtime was created"
              isAlert
              actions={[{ text: "Go to the Runtimes page", to: "/runtimes" }]}
            >
              Check the Runtimes page before you try again, so you don't create it twice.
              {error instanceof ApiError && error.detail && <ErrorDetails details={error.detail} />}
            </SectionMessage>
          )}
          {error !== null && !unconfirmed(error) && <ErrorMessage error={error} />}
        </div>
        <CreateSummary
          choice={choice}
          secrets={fields}
          hasTemplate={chosenTemplate !== null}
          limit={limit}
          problem={problem}
          isPending={pending}
        />
      </form>
    </div>
  );
}
