/**
 * The secrets row's body: every key the project knows about, each with the one provenance that
 * decides what the operator can do about it, then a form for a key no manifest declares yet.
 * A project key also says how a run is supplied it (decision 75): an environment variable, or
 * a header a cloud sandbox's egress proxy sets, the operator's choice written with the value.
 * A reader without `projects:manage` sees every row and how each key is supplied, and no form.
 */

import { type ReactNode, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "../../api/client";
import { useCan } from "../../api/organizationQueries";
import { useDeleteSecret, usePatchSecretDelivery, usePutSecret } from "../../api/queries";
import type {
  ProjectDetail,
  RuntimeProvidedSecret,
  SandboxDelivery,
  SecretDelivery,
  SecretDeliveryWrite,
  SecretWrite,
} from "../../api/types";
import {
  Button,
  Code,
  ConfirmDialog,
  ErrorMessage,
  Form,
  FormFooter,
  Icon,
  Select,
  showFlag,
  TextField,
} from "../ui";
import { EGRESS_ALLOWLIST, listSetting } from "./PermitsBody";
import { FIELD_WIDTH, INTRO } from "./RowMessage";

/** `worker.agent_steps._TOKEN_KEY`: minted per run on GitHub, never stored anywhere. */
const GH_TOKEN = "GH_TOKEN";
/** The project PAT a GitLab ticket source presents in place of a minted token. */
const GITLAB_TOKEN = "GITLAB_TOKEN";
/** What the Tickets page writes for a GitLab source, so each is changed there. Removing the
 *  token here would cut the project off from GitLab unless the deployment sets its own. */
const GITLAB_SOURCE_SECRETS = new Set([GITLAB_TOKEN, "GITLAB_WEBHOOK_SECRET", "GITLAB_CA_BUNDLE"]);

/** The GitHub connection's own secrets: written through the ticket source, never stored here. */
const CONNECTION_SECRETS = new Set([
  "GITHUB_CONNECTION_PRIVATE_KEY",
  "GITHUB_CONNECTION_WEBHOOK_SECRET",
  "GITHUB_CONNECTION_TOKEN",
]);

type SecretRow =
  | { name: string; kind: "minted" }
  | { name: string; kind: "managed"; stored: boolean }
  | { name: string; kind: "runtime"; runtime: RuntimeProvidedSecret; superseded: boolean }
  | { name: string; kind: "project"; stored: boolean; delivery?: SecretDelivery };

/** One row per key the page knows about — declared, runtime-provided, or merely stored — each
 *  with the one provenance that decides what the operator can do about it. */
export function secretRows(project: ProjectDetail): SecretRow[] {
  const runtime = new Map(project.runtime_provided_secrets.map((r) => [r.name, r]));
  const stored = new Set(project.secret_names);
  const deliveries = new Map(project.secret_deliveries.map((d) => [d.name, d]));
  const gitlab = project.ticket_source?.kind === "gitlab";
  const names = [
    ...new Set([
      ...project.declared_secrets,
      ...runtime.keys(),
      ...project.secret_names,
      ...(gitlab ? [GITLAB_TOKEN] : []),
    ]),
  ]
    .filter((name) => !(gitlab && name === GH_TOKEN))
    .sort();
  return names.map((name) => {
    if (name === GH_TOKEN) return { name, kind: "minted" };
    if (CONNECTION_SECRETS.has(name)) return { name, kind: "managed", stored: stored.has(name) };
    const provided = runtime.get(name);
    if (provided) return { name, kind: "runtime", runtime: provided, superseded: stored.has(name) };
    return { name, kind: "project", stored: stored.has(name), delivery: deliveries.get(name) };
  });
}

function rowState(row: SecretRow): "ok" | "missing" {
  if (row.kind === "minted") return "ok";
  if (row.kind === "runtime") return row.runtime.stored ? "ok" : "missing";
  return row.stored ? "ok" : "missing";
}

const STATE_ICON = {
  ok: { glyph: "status-success", label: "Available", ink: "text-icon-success" },
  missing: { glyph: "status-error", label: "Missing", ink: "text-icon-danger" },
} as const;

/** What each choice means, shown under the dropdown once chosen. */
const CHOICES = {
  env: {
    label: "Environment variable",
    description:
      "The agent can read this value and could leak it, for example in a commit. Use a credential scoped to what the agent needs.",
  },
  header: {
    label: "Header through the egress proxy",
    description:
      "In a cloud sandbox the agent sees a placeholder and the egress proxy adds the header. A host run, which has no proxy, gets it as an environment variable.",
  },
} as const;

type DeliveryDraft = { choice: SandboxDelivery; host: string; header: string };

function draftOf(delivery: SecretDelivery | undefined): DeliveryDraft {
  return {
    choice: delivery?.mode ?? "env",
    host: delivery?.host ?? "",
    header: delivery?.header ?? "",
  };
}

function deliveryOf(draft: DeliveryDraft): SecretDeliveryWrite {
  if (draft.choice === "header") {
    return {
      sandbox_delivery: "header",
      egress_host: draft.host.trim(),
      egress_header: draft.header.trim(),
    };
  }
  return { sandbox_delivery: "env" };
}

function writeOf(value: string, draft: DeliveryDraft): SecretWrite {
  return { value, ...deliveryOf(draft) };
}

function sameDraft(a: DeliveryDraft, b: DeliveryDraft): boolean {
  return a.choice === b.choice && a.host === b.host && a.header === b.header;
}

/** The API codes a refused egress host, so it can stand under the host field it is about. */
function hostRefusal(error: unknown): string | undefined {
  return error instanceof ApiError && error.code === "egress_host" ? error.message : undefined;
}

function draftReady(draft: DeliveryDraft): boolean {
  return draft.choice !== "header" || (draft.host.trim() !== "" && draft.header.trim() !== "");
}

/** How a run is supplied the key. The host is picked from the project's egress hosts when
 *  every one is exact; the server refuses a host the list does not cover either way. */
function DeliveryFields({
  project,
  name,
  draft,
  onChange,
  attempted,
  refusal,
}: {
  project: ProjectDetail;
  name: string;
  draft: DeliveryDraft;
  onChange: (draft: DeliveryDraft) => void;
  attempted: boolean;
  refusal?: string;
}) {
  const hosts = listSetting(project.settings, EGRESS_ALLOWLIST);
  const pick = hosts.length > 0 && hosts.every((host) => !host.startsWith("*."));
  // With one exact egress host there is nothing to choose, so a header starts on it.
  const sole = (hosts.length === 1 && pick && hosts[0]) || "";
  const hostError = attempted && draft.host.trim() === "" ? "Choose the host." : refusal;
  const headerError = attempted && draft.header.trim() === "" ? "Enter the header." : undefined;
  return (
    <div className="space-y-3">
      <div className={FIELD_WIDTH.repository}>
        <Select
          label="Supplied as"
          id={`delivery-${name}`}
          data-delivery
          value={draft.choice}
          onChange={(event) => {
            const choice = event.target.value as DeliveryDraft["choice"];
            const host = choice === "header" && !draft.host ? sole : draft.host;
            onChange({ ...draft, choice, host });
          }}
          helperMessage={
            draft.choice === "env" ? (
              <span data-tone="warning" className="inline-flex items-start gap-1 text-warning">
                <Icon glyph="status-warning" size="small" className="mt-0.5" />
                {CHOICES.env.description}
              </span>
            ) : (
              CHOICES.header.description
            )
          }
          options={[
            { value: "env", label: CHOICES.env.label },
            hosts.length > 0
              ? { value: "header", label: CHOICES.header.label }
              : {
                  value: "header",
                  label: `${CHOICES.header.label} (add an egress host first)`,
                  isDisabled: true,
                },
          ]}
        />
      </div>
      {draft.choice === "header" && (
        <div className="ml-6 flex flex-wrap items-start gap-3">
          <div className={FIELD_WIDTH.name}>
            {pick ? (
              <Select
                label="Egress host"
                data-egress-host
                value={draft.host}
                onChange={(event) => onChange({ ...draft, host: event.target.value })}
                helperMessage="From this project's egress hosts"
                errorMessage={hostError}
                options={[
                  { value: "", label: "Choose a host" },
                  ...[...new Set([...hosts, ...(draft.host ? [draft.host] : [])])].map((h) => ({
                    value: h,
                    label: h,
                  })),
                ]}
              />
            ) : (
              <TextField
                label="Egress host"
                data-egress-host
                isMonospaced
                autoComplete="off"
                value={draft.host}
                onChange={(event) => onChange({ ...draft, host: event.target.value })}
                helperMessage="One of this project's egress hosts, under Runtime"
                errorMessage={hostError}
              />
            )}
          </div>
          <div className={FIELD_WIDTH.name}>
            <TextField
              label="Header"
              data-egress-header
              isMonospaced
              autoComplete="off"
              value={draft.header}
              onChange={(event) => onChange({ ...draft, header: event.target.value })}
              helperMessage="The value becomes the header's whole value"
              errorMessage={headerError}
            />
          </div>
        </div>
      )}
    </div>
  );
}

/** A stored secret's delivery, changed in place: no value to type again. Back to an environment
 *  variable saves as soon as it is chosen; a header waits for its host and header. */
function DeliveryEditor({
  project,
  name,
  delivery,
}: {
  project: ProjectDetail;
  name: string;
  delivery?: SecretDelivery;
}) {
  const patch = usePatchSecretDelivery(project.id);
  const saved = draftOf(delivery);
  const [draft, setDraft] = useState(saved);
  const [attempted, setAttempted] = useState(false);
  const save = (next: DeliveryDraft) =>
    patch.mutate(
      { name, ...deliveryOf(next) },
      {
        onSuccess: () => {
          setAttempted(false);
          showFlag({ appearance: "success", title: `Saved how ${name} is supplied` });
        },
      },
    );

  return (
    <div className="ml-6 space-y-2" data-delivery-for={name}>
      <DeliveryFields
        project={project}
        name={name}
        draft={draft}
        attempted={attempted}
        refusal={hostRefusal(patch.error)}
        onChange={(next) => {
          patch.reset();
          setDraft(next);
          if (next.choice === "env" && saved.choice !== "env") save(next);
        }}
      />
      {draft.choice === "header" && !sameDraft(draft, saved) && (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            appearance="primary"
            isLoading={patch.isPending}
            onClick={() => {
              setAttempted(true);
              if (draftReady(draft)) save(draft);
            }}
          >
            Save
          </Button>
          <Button
            appearance="subtle"
            onClick={() => {
              patch.reset();
              setAttempted(false);
              setDraft(saved);
            }}
          >
            Cancel
          </Button>
        </div>
      )}
      <ErrorMessage error={hostRefusal(patch.error) ? null : patch.error} />
    </div>
  );
}

/** How a stored key is supplied, said rather than offered, to a reader who may not change it. */
function DeliveryNote({ name, delivery }: { name: string; delivery?: SecretDelivery }) {
  return (
    <p className="ml-6 text-body-sm text-subtle" data-delivery-for={name}>
      {delivery?.mode === "header" ? (
        <>
          Supplied as the header <Code>{delivery.header ?? ""}</Code> on{" "}
          <Code>{delivery.host ?? ""}</Code>, through the egress proxy.
        </>
      ) : (
        "Supplied as an environment variable."
      )}
    </p>
  );
}

/** Config is pinned per ticket: a ticket admitted before the process named the runtime keeps
 *  reading the project copy, so Remove must say when that is still the case. */
function stillReadNote(openTickets: number): string {
  if (openTickets === 0) return "";
  const one = openTickets === 1;
  return ` But ${openTickets} open ${one ? "ticket" : "tickets"} started before the change still ${one ? "uses" : "use"} this copy. Remove it after ${one ? "that ticket finishes" : "they finish"}.`;
}

function RuntimeLink({ slug }: { slug: string }) {
  return (
    <Link to={`/runtimes/${slug}`} className="font-medium underline underline-offset-2">
      {slug}
    </Link>
  );
}

/** What Remove deletes and what stops working. */
function removalOf(row: Exclude<SecretRow, { kind: "minted" }>) {
  if (row.kind === "runtime") {
    return {
      title: `Remove the stored copy of ${row.name}?`,
      consequence: (
        <>
          Factorial deletes the value stored on this project. You can't undo this. Runtime{" "}
          {row.runtime.runtime_slug} keeps providing its own key.
          {stillReadNote(row.runtime.open_tickets_reading_copy)}
        </>
      ),
      confirmLabel: "Remove copy",
      done: `Removed the stored copy of ${row.name}`,
    };
  }
  return {
    title: `Remove ${row.name}?`,
    consequence:
      "Factorial deletes the value stored on this project and how it is supplied. You can't undo this. Until you save it again, a run that needs it starts without it, a run whose MCP server uses it can't start, and a preview that uses it is refused.",
    confirmLabel: "Remove",
    done: `Removed ${row.name}`,
  };
}

function SecretRowView({ project, row }: { project: ProjectDetail; row: SecretRow }) {
  const remove = useDeleteSecret(project.id);
  const canManage = useCan("projects:manage");
  const [confirming, setConfirming] = useState(false);
  const [replacing, setReplacing] = useState(false);
  const state = rowState(row);
  const icon = STATE_ICON[state];
  const onTickets = project.ticket_source?.kind === "gitlab" && GITLAB_SOURCE_SECRETS.has(row.name);
  const removal = confirming && row.kind !== "minted" ? removalOf(row) : null;

  let provenance: ReactNode;
  if (row.kind === "minted") {
    provenance = "Created automatically for each run. Nothing to store.";
  } else if (row.kind === "managed") {
    provenance = "Managed with the GitHub connection, under Tickets.";
  } else if (row.kind === "runtime") {
    provenance = (
      <>
        Provided by runtime <RuntimeLink slug={row.runtime.runtime_slug} />
        {row.runtime.stored
          ? "."
          : ". The runtime holds no key yet. Save it on the runtime's page."}
      </>
    );
  } else if (row.stored && onTickets) {
    provenance = (
      <>
        Stored on this project. Change it on the{" "}
        <Link
          to={`/projects/${project.id}/configuration/ticket-source`}
          className="font-medium underline underline-offset-2"
        >
          Tickets
        </Link>{" "}
        page.
      </>
    );
  } else {
    provenance = row.stored
      ? "Stored on this project."
      : canManage
        ? "Missing. Save it below."
        : "Missing.";
  }

  return (
    <div
      className="space-y-2 border-t border-default py-2 first:border-t-0"
      data-secret-state={state}
    >
      <div className="flex items-start gap-2 text-body">
        <Icon glyph={icon.glyph} label={icon.label} className={`mt-0.5 ${icon.ink}`} />
        <Code>{row.name}</Code>
        <span className="text-body-sm text-subtle">{provenance}</span>
        {canManage && row.kind === "project" && row.stored && !replacing && (
          <Button appearance="subtle" spacing="compact" onClick={() => setReplacing(true)}>
            Replace value
          </Button>
        )}
        {canManage && row.kind === "project" && row.stored && !replacing && !onTickets && (
          <Button
            appearance="danger"
            spacing="compact"
            data-remove={row.name}
            onClick={() => setConfirming(true)}
          >
            Remove
          </Button>
        )}
      </div>
      {row.kind === "project" &&
        row.stored &&
        !replacing &&
        (canManage ? (
          <DeliveryEditor project={project} name={row.name} delivery={row.delivery} />
        ) : (
          <DeliveryNote name={row.name} delivery={row.delivery} />
        ))}
      {row.kind === "runtime" && row.superseded && (
        <div className="ml-6 flex flex-wrap items-center gap-2 text-body-sm text-subtle">
          <span>
            A copy is also stored on this project, superseded by runtime{" "}
            <RuntimeLink slug={row.runtime.runtime_slug} />. No process that names the runtime reads
            it.{stillReadNote(row.runtime.open_tickets_reading_copy)}
          </span>
          {canManage && (
            <Button
              appearance="danger"
              spacing="compact"
              data-remove={row.name}
              onClick={() => setConfirming(true)}
            >
              Remove copy
            </Button>
          )}
        </div>
      )}
      {removal && (
        <ConfirmDialog
          title={removal.title}
          consequence={removal.consequence}
          confirmLabel={removal.confirmLabel}
          isPending={remove.isPending}
          error={remove.error}
          onCancel={() => {
            remove.reset();
            setConfirming(false);
          }}
          onConfirm={() =>
            remove.mutate(row.name, {
              onSuccess: () => {
                setConfirming(false);
                showFlag({ appearance: "success", title: removal.done });
              },
            })
          }
        />
      )}
      {canManage && row.kind === "project" && (!row.stored || replacing) && (
        <StoreForm
          project={project}
          name={row.name}
          delivery={row.delivery}
          valueOnly={replacing}
          onDone={() => setReplacing(false)}
          onCancel={replacing ? () => setReplacing(false) : undefined}
        />
      )}
    </div>
  );
}

/** The PUT form for one named key: the value and how it is supplied, the name is the row's. A
 *  write replaces the whole row, so `valueOnly` (Replace value) sends the stored delivery again. */
function StoreForm({
  project,
  name,
  delivery,
  valueOnly = false,
  onDone,
  onCancel,
}: {
  project: ProjectDetail;
  name: string;
  delivery?: SecretDelivery;
  valueOnly?: boolean;
  onDone: () => void;
  onCancel?: () => void;
}) {
  const put = usePutSecret(project.id);
  const [value, setValue] = useState("");
  const [draft, setDraft] = useState(() => draftOf(delivery));

  return (
    <Form
      onSubmit={() =>
        put.mutate(
          { name, ...writeOf(value, draft) },
          {
            onSuccess: () => {
              setValue("");
              onDone();
            },
          },
        )
      }
      validate={() => value !== "" && draftReady(draft)}
      className="ml-6 space-y-3"
    >
      {({ attempted }) => (
        <div data-store-for={name} className="space-y-3">
          <div className="w-full max-w-sm">
            <TextField
              label={`Value for ${name}`}
              labelHidden
              type="password"
              autoComplete="new-password"
              placeholder={`Value for ${name}`}
              value={value}
              onChange={(event) => setValue(event.target.value)}
              errorMessage={attempted && value === "" ? "Enter a value to store." : undefined}
            />
          </div>
          {!valueOnly && (
            <DeliveryFields
              project={project}
              name={name}
              draft={draft}
              onChange={setDraft}
              attempted={attempted}
              refusal={hostRefusal(put.error)}
            />
          )}
          <div className="flex flex-wrap items-center gap-2">
            <Button type="submit" appearance="primary" isLoading={put.isPending}>
              Save
            </Button>
            {onCancel && (
              <Button appearance="subtle" onClick={onCancel}>
                Cancel
              </Button>
            )}
          </div>
          <ErrorMessage error={hostRefusal(put.error) ? null : put.error} className="w-full" />
        </div>
      )}
    </Form>
  );
}

export function SecretsBody({ project }: { project: ProjectDetail }) {
  const put = usePutSecret(project.id);
  const canManage = useCan("projects:manage");
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [draft, setDraft] = useState(() => draftOf(undefined));
  const rows = secretRows(project);

  return (
    <div>
      {/* What the project HOLDS, above what it is about to be given. A rule between the two, so
          the rows above read as a state and the fields below as an action on it. */}
      <div className={canManage ? "space-y-3 border-b border-default p-4" : "space-y-3 p-4"}>
        <p className={INTRO}>
          Secrets are encrypted and can&apos;t be viewed after you save them. To change one, save it
          again. Model provider keys are set on the runtime&apos;s page.
        </p>
        {rows.length > 0 && (
          <div>
            {rows.map((row) => (
              <SecretRowView key={row.name} project={project} row={row} />
            ))}
          </div>
        )}
      </div>

      {canManage && (
        <Form
          // Cleared only on success — a rejected write must leave the value in the box, or the
          // operator has to fetch the credential again.
          onSubmit={() =>
            put.mutate(
              { name: name.trim(), ...writeOf(value, draft) },
              {
                onSuccess: () => {
                  setName("");
                  setValue("");
                  setDraft(draftOf(undefined));
                },
              },
            )
          }
          validate={() => name.trim() !== "" && value !== "" && draftReady(draft)}
          className="space-y-3 p-4"
        >
          {({ attempted }) => (
            <>
              <p className={INTRO}>
                Save a key that no manifest declares yet, such as an MCP server's token.
              </p>
              {/* `off` and `new-password` keep the browser from filling a signed-in address and a
                saved password into a key and its value — Chrome ignores `off` on a password. */}
              <div className={FIELD_WIDTH.name}>
                <TextField
                  label="Name"
                  isRequired
                  autoComplete="off"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="PRODUCT_DOCS_TOKEN"
                  isMonospaced
                  errorMessage={
                    attempted && name.trim() === "" ? "Enter the key's name." : undefined
                  }
                />
              </div>
              <div className={FIELD_WIDTH.repository}>
                <TextField
                  label="Value"
                  isRequired
                  type="password"
                  autoComplete="new-password"
                  value={value}
                  onChange={(event) => setValue(event.target.value)}
                  errorMessage={attempted && value === "" ? "Enter a value to store." : undefined}
                />
              </div>
              {/* A key is env-var shaped, so no key's field ids can collide with these. */}
              <DeliveryFields
                project={project}
                name="new-secret"
                draft={draft}
                onChange={setDraft}
                attempted={attempted}
                refusal={hostRefusal(put.error)}
              />
              <ErrorMessage error={hostRefusal(put.error) ? null : put.error} />
              <FormFooter>
                <Button type="submit" appearance="primary" isLoading={put.isPending}>
                  Save secret
                </Button>
              </FormFooter>
            </>
          )}
        </Form>
      )}
    </div>
  );
}
