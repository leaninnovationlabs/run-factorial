/**
 * Create a runtime: a CLI, how it signs in and where it runs, each chosen on cards, then its keys
 * and its name. Its own page, like `/projects/new`: the slug it fixes is the one thing a repo
 * will depend on.
 *
 * The cards are what this deployment offers (`GET /api/runtime-options`), and the API refuses
 * the rest. Until that list is read, the page shows no cards. One request stores the runtime and
 * its keys, all or nothing. The keys are read off the form only inside the Test and Create
 * handlers and go straight through the API client, never through React state or a query
 * mutation. A Codex runtime that signs in with ChatGPT opens its own page with
 * `?connect=chatgpt`, which starts the device login there.
 *
 * The name and the slug follow the cards until the operator edits one; then the slug follows the
 * name. `?slug=` presets the slug, and the cards when it has the shape of a suggested one.
 */

import { useQueryClient } from "@tanstack/react-query";
import { type FormEvent, type ReactNode, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ApiError, api } from "../api/client";
import { keys, useRuntimeOptions } from "../api/queries";
import type { Runtime, RuntimeCheckOut } from "../api/types";
import { CreateCredentials } from "../components/runtimes/CreateCredentials";
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
  choiceFromSlug,
  choiceLabels,
  choiceProblem,
  defaultChoice,
  deferredLogin,
  detailsFor,
  fieldsFor,
  ISOLATION_FOR,
  reconcile,
  SANDBOX_CARDS,
  type Sandbox,
  suggestName,
  suggestSlugFor,
  typeFor,
} from "../components/runtimes/runtimeChoice";
import { defaultBuild, orderBuilds, SLUG_RE, suggestSlug } from "../components/runtimes/runtimes";
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

const FALLBACK: Choice = { cli: "claude", auth: "api_key", sandbox: "host" };

/** A Test outcome for one `credentialsVersion`, with the template picked from its builds. */
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
  const form = useRef<HTMLFormElement>(null);
  // A second press before the first re-render must not send a second create.
  const inFlight = useRef(false);

  const [params] = useSearchParams();
  const presetSlug = useMemo(() => {
    const wanted = params.get("slug");
    return wanted !== null && SLUG_RE.test(wanted) ? wanted : null;
  }, [params]);
  const [picked, setPicked] = useState<Choice | null>(null);
  const [codexSignIn, setCodexSignIn] = useState<CodexSignIn>("chatgpt");
  const [nameEdit, setNameEdit] = useState<string | null>(null);
  const [slugEdit, setSlugEdit] = useState<string | null>(presetSlug);
  const [credentialsVersion, setCredentialsVersion] = useState(0);
  const [check, setCheck] = useState<Check | null>(null);
  const [checking, setChecking] = useState(false);
  const [attempted, setAttempted] = useState(false);
  const [missing, setMissing] = useState<ReadonlySet<string>>(new Set());
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [slugConflict, setSlugConflict] = useState<{ slug: string; message: string } | null>(null);

  const initial = {
    ...((offered && defaultChoice(offered)) ?? FALLBACK),
    ...(presetSlug ? choiceFromSlug(presetSlug) : null),
  };
  const choice = reconcile(offered, picked ?? initial);
  const states = cardStates(offered, choice);
  const type = typeFor(choice.cli, choice.auth);
  const isolation = ISOLATION_FOR[choice.sandbox];
  const target = type && isolation ? { type, isolation } : null;
  const fields = fieldsFor(choice, codexSignIn);
  const current = check?.version === credentialsVersion ? check : null;
  const result = current?.result ?? null;
  const templates =
    choice.sandbox === "cloud" && result?.e2b?.ok ? orderBuilds(result.templates) : null;
  const chosenTemplate = (templates && current?.template) || null;
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

  function credentialsChanged() {
    setCredentialsVersion((version) => version + 1);
    setCheck(null);
    setMissing(new Set());
  }

  function pick(next: Partial<Choice>) {
    setPicked({ ...choice, ...next });
    credentialsChanged();
  }

  function readCredentials(element: HTMLFormElement): Record<string, string> {
    const data = new FormData(element);
    return Object.fromEntries(fields.map((each) => [each, String(data.get(each) ?? "")]));
  }

  async function runCheck() {
    if (!form.current || target === null) return;
    const credentials = Object.fromEntries(
      Object.entries(readCredentials(form.current)).filter(([, value]) => value.trim() !== ""),
    );
    const version = credentialsVersion;
    setChecking(true);
    setCheck((previous) => previous && { ...previous, error: null });
    try {
      const checked = await api.checkRuntime({
        type: target.type,
        details: detailsFor(choice),
        credentials,
      });
      const template = defaultBuild(orderBuilds(checked.templates))?.pin ?? null;
      setCheck({ version, result: checked, error: null, template });
    } catch (caught) {
      setCheck({
        version,
        result: null,
        error: unconfirmed(caught) ? "Couldn't check the runtime. Try again." : caught,
        template: null,
      });
    } finally {
      setChecking(false);
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

  if (options.isPending) {
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
          </Step>
          <Step n={3} title="Sandbox" note="Where each run's code executes.">
            <RadioGroup
              appearance="tile"
              label="Sandbox"
              name="sandbox"
              value={choice.sandbox}
              onChange={(value) => pick({ sandbox: value as Sandbox })}
              options={cardOptions(SANDBOX_CARDS, states.sandbox)}
            />
          </Step>
          <Step
            n={4}
            title="Credentials"
            note="Stored encrypted. You can't view them again. Nothing is stored until you create the runtime."
          >
            <CreateCredentials
              choice={choice}
              fields={fields}
              codexSignIn={codexSignIn}
              onCodexSignIn={(value) => {
                setCodexSignIn(value);
                credentialsChanged();
              }}
              onEdit={credentialsChanged}
              missing={missing}
              templates={templates}
              template={chosenTemplate}
              onTemplate={(pin) =>
                setCheck((previous) => previous && { ...previous, template: pin })
              }
              test={{
                onTest: runCheck,
                isPending: checking,
                result,
                error: current?.error ?? null,
              }}
            />
          </Step>
          <Step n={5} title="Name">
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
          problem={problem}
          isPending={pending}
        />
      </form>
    </div>
  );
}
