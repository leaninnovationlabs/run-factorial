/**
 * The bodies of the configuration pages: the project, the repository its work lands in and what
 * that repository must carry, where its tickets come from, which runtime each process runs on.
 * `ReadinessLedger` provides the geography; each body here is one page's forms.
 *
 * Each form saves one thing with its own submit and its own error, because each fails on its own —
 * a repository the App cannot see, a ticket source that is not owner/name. A reader without
 * `projects:manage` gets each form's values read-only, with no submit and no write-only field.
 *
 * What is wrong with a page's requirements is not said here. The page leads with it, in one box
 * above the body, with each fix as a button — the command to copy, the settings to open, the
 * labels to create (`PageMessages` in `RowMessage.tsx`). The secrets body is `SecretsBody.tsx`.
 */

import { type ReactNode, useState } from "react";
import { ApiError } from "../../api/client";
import { useCan, useMultiOrg } from "../../api/organizationQueries";
import {
  useGitHubConnection,
  useProjectLabels,
  usePutGitHubConnection,
  usePutSecret,
  usePutTicketSource,
  useRuntimes,
  useUpdateProject,
} from "../../api/queries";
import type {
  GitHubConnectionIn,
  ProjectDetail,
  ReadinessCheck,
  ReadinessItem,
} from "../../api/types";
import { type ForgeHost, forgeHost, forgeName } from "../../forge";
import { formatRelative } from "../../format";
import { ROLE_ICON } from "../../status";
import { unpermitted } from "../process/commitGate";
import {
  Button,
  Code,
  CopyButton,
  cx,
  EmptyState,
  ErrorMessage,
  Form,
  FormFooter,
  Icon,
  LinkButton,
  SegmentedControl,
  Select,
  ServerText,
  TextArea,
  TextField,
} from "../ui";
import { filterLabels, type LabelFilter, labelCounts } from "./labelFilter";
import { listSetting, RUNTIME_ALLOWLIST, usePermitChange } from "./PermitsBody";
import { FIELD_WIDTH, INTRO, RemedyCommand } from "./RowMessage";
import { CHECKING, ROW_TITLES, readinessOf, UNCHECKED } from "./readiness";

// ---------------------------------------------------------------- readiness

const LABEL_FILTERS: { value: LabelFilter; label: string }[] = [
  { value: "missing", label: "Missing" },
  { value: "present", label: "Present" },
  { value: "all", label: "All" },
];

function isConflict(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409;
}

function LabelsBody({ project }: { project: ProjectDetail }) {
  const labels = useProjectLabels(project.id);
  const [filter, setFilter] = useState<LabelFilter>("missing");

  const all = labels.data?.labels ?? [];
  const counts = labelCounts(all);
  const shown = filterLabels(all, filter);

  return (
    <div className="space-y-3">
      {labels.data && (
        <>
          <SegmentedControl
            label="Show labels"
            value={filter}
            onChange={setFilter}
            options={LABEL_FILTERS.map((option) => ({ ...option, count: counts[option.value] }))}
          />
          {shown.length === 0 ? (
            <p className={INTRO}>
              {filter === "missing"
                ? "The repository defines every label this project's processes use."
                : "No labels match this filter."}
            </p>
          ) : (
            <ul className="divide-y divide-default rounded-md border border-default">
              {shown.map((label) => (
                <li key={label.name} className="flex items-center gap-2 px-3 py-1.5 text-body">
                  {/* GitHub's own colour for the label, a fact about the repository rather than
                      a colour of this app's. */}
                  <span
                    aria-hidden
                    className="size-2.5 shrink-0 rounded-full"
                    style={{ backgroundColor: `#${label.color}` }}
                  />
                  <Code>{label.name}</Code>
                  <span className="min-w-0 flex-1 truncate text-body-sm text-subtle">
                    {label.description}
                  </span>
                  {label.exists === false && (
                    <Icon
                      glyph="status-warning"
                      label="Missing from the repository"
                      className="text-icon-warning"
                    />
                  )}
                  {label.exists === true && (
                    <Icon
                      glyph="status-success"
                      label="Defined in the repository"
                      className="text-icon-success"
                    />
                  )}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
      {/* A 409 is the API's answer that no process is synced yet; any other failure is not, and
          says what it is. */}
      {isConflict(labels.error) ? (
        <EmptyState
          headingLevel={3}
          header="No process config yet"
          description="Sync a process to see the labels it uses."
        />
      ) : (
        <ErrorMessage
          error={labels.error}
          title="Couldn't load the labels"
          onRetry={() => labels.refetch()}
        />
      )}
    </div>
  );
}

/** The rules on the two branches the work moves through, one check per setting, each failing
 *  one beside the command that fixes it alone. */
function BranchProtectionBody({
  forge,
  protection,
  configRules,
}: {
  forge: ForgeHost;
  protection: ReadinessItem | undefined;
  configRules: ReadinessItem | undefined;
}) {
  const gitlab = forge.kind === "gitlab";
  // Numbered by what is shown: under `policy: ignored` the default branch has no row.
  const groups = [
    protection && {
      title: "Default branch",
      intro: `Branch protection makes sure a person approves every change before it reaches the default branch. Factorial opens the ${gitlab ? "merge requests" : "pull requests"}, so it can't approve them itself.`,
      row: protection,
      label: "Command to protect the branch",
    },
    configRules && {
      title: (
        <>
          Config branch <Code>factorial</Code>
        </>
      ),
      intro: "Config is synced from this branch, and the builder commits to it directly.",
      row: configRules,
      label: "Command to protect the factorial branch",
    },
  ].filter((group) => !!group);
  return (
    <div className="space-y-4">
      {/* No fix button, on purpose. Applying protection needs `administration: write`, which
          would let the App remove the control it is being checked against. */}
      <p className={INTRO}>
        {gitlab
          ? "GitLab applies these rules, and Factorial only reads them with the project's token, so each fix is a command you run with your own GitLab credentials."
          : "GitHub applies these rules, and the App only reads them. An App that can change branch protection can also remove it, so each fix is a command you run with your own GitHub credentials."}
      </p>
      {groups.map((group, index) => (
        <BranchChecks key={group.row.key} number={index + 1} {...group} />
      ))}
    </div>
  );
}

function BranchChecks({
  number,
  title,
  intro,
  row,
  label,
}: {
  number: number;
  title: ReactNode;
  intro: string;
  row: ReadinessItem;
  /** For the row's own command, shown while its settings could not be read one by one. */
  label: string;
}) {
  const checks = row.checks ?? [];
  return (
    <div data-branch-checks={row.key} className="space-y-2">
      <div className="space-y-1">
        <h4 className="text-heading-xs text-default">
          {number}. {title}
        </h4>
        <RowState row={row} />
      </div>
      <p className={INTRO}>{intro}</p>
      {checks.length > 0 ? (
        <ul className="divide-y divide-default rounded-md border border-default">
          {checks.map((check) => (
            <CheckItem key={check.key} check={check} />
          ))}
        </ul>
      ) : (
        row.remedy && <RemedyCommand remedy={row.remedy} label={label} />
      )}
    </div>
  );
}

function CheckItem({ check }: { check: ReadinessCheck }) {
  const icon = ROLE_ICON[check.passed ? "success" : "warning"];
  const failing = !check.passed;
  return (
    <li data-check={check.key} className="flex gap-2 px-3 py-2">
      <Icon
        glyph={icon.glyph}
        label={check.passed ? "Passes" : "Fails"}
        size="small"
        className={cx("mt-1", icon.ink)}
      />
      <div className="min-w-0 flex-1 space-y-1">
        <p className="text-body font-medium text-default">
          <ServerText text={check.title} />
        </p>
        <p className="text-body-sm text-subtle">
          <ServerText text={check.description} />
        </p>
        {failing && check.remedy && (
          <RemedyCommand remedy={check.remedy} label="Command to fix it" name={check.title} />
        )}
        {failing && check.link && (
          <LinkButton href={check.link} spacing="compact">
            Open on GitHub
          </LinkButton>
        )}
      </div>
    </li>
  );
}

//: The repository host is not chosen here: the ticket source stores the forge, and this shows it.
const REPOSITORY_HOSTS = [
  { value: "github", label: "GitHub" },
  { value: "gitlab", label: "GitLab" },
];
//: Trackers the page names, so a reader sees where support is going; Jira is not built, so it
//: cannot be chosen.
const TICKET_TRACKERS = [
  { value: "github", label: "GitHub Issues" },
  { value: "gitlab", label: "GitLab Issues" },
  { value: "jira", label: "Jira (not supported yet)", isDisabled: true },
];

// The API's own patterns: owner/name on GitHub, any depth of nested groups on GitLab.
const REPO_PATTERN = /^[^/\s]+\/[^/\s]+$/;
const GITLAB_REPO_PATTERN = /^[^/\s]+(?:\/[^/\s]+)+$/;

/** `owner/name` from what people paste — a repository's URL, a clone URL, an SSH remote — as the
 *  API's own normalization reads it, so the field can ask for a URL and still check it here. */
export function repoName(value: string): string {
  let text = value.trim();
  for (const prefix of ["https://github.com/", "http://github.com/", "git@github.com:"]) {
    if (text.startsWith(prefix)) text = text.slice(prefix.length);
  }
  return text.replace(/\/+$/, "").replace(/\.git$/, "");
}

const repoProblemOf = (value: string, forge: ForgeHost["kind"]) => {
  if (forge === "gitlab") {
    return GITLAB_REPO_PATTERN.test(repoName(value))
      ? null
      : "Use group/project, including nested groups.";
  }
  return REPO_PATTERN.test(repoName(value)) ? null : "Use a GitHub URL or owner/name.";
};

/** Everything about the repository the project's work lands in: which one it is, and what it
 *  must carry — the App's reach into it, the rules on its default and config branches, the CI,
 *  who may approve a gate. One page, because all of it is set up once, in the repository host. */
export function RepositorySetupBody({
  project,
  rows,
}: {
  project: ProjectDetail;
  rows: ReadinessItem[];
}) {
  const row = (key: ReadinessItem["key"]) => rows.find((each) => each.key === key);
  const access = row("app_access");
  const protection = row("branch_protection");
  const configRules = row("config_branch_rules");
  const ci = row("ci_workflows");
  const approvers = row("gate_approvers");
  const forge = forgeHost(project.ticket_source, project.settings);
  const [repo, setRepo] = useState(project.github_repo);
  // The checks read the saved repository, so they wait until the form holds it: a URL being typed
  // or edited is not yet the repository they describe.
  const saved = !repoProblemOf(repo, forge.kind) && repoName(repo) === project.github_repo;
  return (
    <div className="divide-y divide-default">
      <Subsection title="Repository">
        <RepositoryForm
          project={project}
          access={access}
          forge={forge}
          repo={repo}
          onRepo={setRepo}
        />
      </Subsection>
      {saved && (protection || configRules) && (
        <Subsection title="Branch protection">
          <BranchProtectionBody forge={forge} protection={protection} configRules={configRules} />
        </Subsection>
      )}
      {ci && (
        <Subsection title={ROW_TITLES.ci_workflows} row={ci}>
          <p className={INTRO}>
            This process waits for CI checks to pass. With no CI workflows in the repository, those
            steps never pass and every ticket times out there.
            {ci.state !== "ok" && " Add a CI workflow."}
          </p>
          {ci.state === "ok" && ci.detail && (
            <p className="text-body text-default">
              <ServerText text={ci.detail} />
            </p>
          )}
        </Subsection>
      )}
      {approvers && (
        <Subsection title={ROW_TITLES.gate_approvers} row={approvers}>
          <p className={INTRO}>
            A gate with <Code>approverPermission</Code> lets anyone holding that repository role
            approve it. When the branch it merges into also requires a code-owner review,{" "}
            {forgeName(forge)} refuses the merge until a code owner approves, and the gate waits and
            comments meanwhile.
          </p>
        </Subsection>
      )}
    </div>
  );
}

function RepositoryForm({
  project,
  access,
  forge,
  repo,
  onRepo,
}: {
  project: ProjectDetail;
  /** The `app_access` row: said under the field it is about, and only when it is not ready. */
  access: ReadinessItem | undefined;
  forge: ForgeHost;
  repo: string;
  onRepo: (repo: string) => void;
}) {
  const update = useUpdateProject(project.id);
  const canManage = useCan("projects:manage");
  const gitlab = forge.kind === "gitlab";
  const repoProblem = repoProblemOf(repo, forge.kind);
  const changed = repoName(repo) !== project.github_repo;
  return (
    <Form
      // Enter in a read-only field still submits the form.
      onSubmit={() => canManage && update.mutate({ github_repo: repoName(repo) })}
      validate={() => !repoProblem}
      className="space-y-4"
    >
      {({ attempted }) => (
        <>
          <div className={FIELD_WIDTH.name}>
            <Select
              label="Repository host"
              options={REPOSITORY_HOSTS}
              value={forge.kind}
              disabled
              helperMessage="Set with the ticket source."
            />
          </div>
          <div className={FIELD_WIDTH.repository}>
            <TextField
              label={gitlab ? "GitLab project" : "Repository URL"}
              isRequired
              readOnly={!canManage}
              value={repo}
              onChange={(event) => onRepo(event.target.value)}
              placeholder={gitlab ? "group/project" : "https://github.com/acme/widgets"}
              isMonospaced
              helperMessage="Where the artifacts and the .factorial/ config live. A change applies from here on: synced process versions keep the repository they came from."
              errorMessage={
                (attempted || repo.trim() !== "") && repoProblem ? repoProblem : undefined
              }
            />
            {access && <AccessNote row={access} />}
          </div>
          {canManage && (
            <>
              <ErrorMessage error={update.error} title="Couldn't save the repository" />
              <FormFooter>
                <Button
                  type="submit"
                  appearance="primary"
                  isDisabled={!changed}
                  isLoading={update.isPending}
                >
                  Save repository
                </Button>
              </FormFooter>
            </>
          )}
        </>
      )}
    </Form>
  );
}

/** Whether the App can reach the repository, under the field that names it — and nothing at all
 *  when it can. A placeholder for a probe still on its way says nothing either. */
function AccessNote({ row }: { row: ReadinessItem }) {
  if (row.state === "ok" || row.summary === CHECKING || row.summary === UNCHECKED) return null;
  if (row.state === "unknown") {
    return (
      <div className="pt-2">
        <RowState row={row} />
      </div>
    );
  }
  const icon = ROLE_ICON[row.state === "blocked" ? "danger" : "warning"];
  return (
    <div data-access-note className="flex items-start gap-1.5 pt-2 text-body-sm">
      <Icon glyph={icon.glyph} size="small" className={cx("mt-0.5", icon.ink)} />
      <div className="min-w-0 space-y-2">
        <p className="text-default">
          <ServerText text={row.detail ?? row.summary} />
        </p>
        {row.checks && row.checks.length > 1 && (
          <ul className="space-y-0.5" data-access-repos>
            {row.checks.map((check) => (
              <li key={check.key} className="flex items-center gap-1.5">
                <Icon
                  glyph={check.passed ? "status-success" : "status-error"}
                  label={check.passed ? "Reachable" : "Not in reach"}
                  size="small"
                  className={check.passed ? "text-icon-success" : "text-icon-danger"}
                />
                <Code>{check.title}</Code>
                <span className="text-subtle">{check.description}</span>
              </li>
            ))}
          </ul>
        )}
        {row.link && (
          <LinkButton href={row.link} spacing="compact">
            Open installation settings
          </LinkButton>
        )}
      </div>
    </div>
  );
}

/** Where the project's tickets come from — the tracker, and the place in it — and the labels its
 *  issues must define, since a ticket's state shows on its issue through them. */
export function TicketSourceBody({
  project,
  rows,
}: {
  project: ProjectDetail;
  rows: ReadinessItem[];
}) {
  const labels = rows.find((row) => row.key === "labels");
  const connection = rows.find((row) => row.key === "github_connection");
  const webhook = rows.find((row) => row.key === "github_webhook");
  return (
    <div className="divide-y divide-default">
      <Subsection title={ROW_TITLES.ticket_source}>
        <TicketSourceForm project={project} connection={connection} webhook={webhook} />
      </Subsection>
      {labels && (
        <Subsection title={ROW_TITLES.labels} row={labels}>
          <LabelsBody project={project} />
        </Subsection>
      )}
    </div>
  );
}

const GITHUB_MODES = [
  { value: "app", label: "GitHub App" },
  { value: "token", label: "Access token" },
];

const TOKEN_CONSEQUENCES =
  "Every agent run of this project gets this token, and it does not expire on its own. It may reach more than this repository. Factorial cannot check that it lacks the workflows permission. Whether its account can approve its own pull requests depends on that account's rights.";

const TOKEN_PERMISSIONS =
  "A fine-grained token for these repositories with: Contents, Issues and Pull requests read and write; Checks and Metadata read.";

/** What the GitHub connection holds in the form: the stored values until the operator types over
 *  them. A secret is never read back, so each starts blank and is sent only when typed. */
function useConnectionDraft(projectId: number) {
  const stored = useGitHubConnection(projectId).data;
  const [mode, setMode_] = useState<"app" | "token" | null>(null);
  const [appId, setAppId_] = useState<string | null>(null);
  const [botTyped, setBotTyped] = useState<string | null>(null);
  const [privateKey, setPrivateKey] = useState("");
  const [webhookSecret, setWebhookSecret] = useState("");
  const [token, setToken] = useState("");
  const current = {
    mode: mode ?? stored?.mode ?? "app",
    appId: appId ?? stored?.app_id ?? "",
    bot: botTyped ?? stored?.bot_login ?? "",
  };
  // An App's login is read from GitHub; only a token project's can be named by hand.
  const botEdited =
    current.mode === "token" && botTyped !== null && botTyped.trim() !== (stored?.bot_login ?? "");
  const setMode = (value: "app" | "token") => {
    setMode_(value);
    setBotTyped(null);
  };
  const setAppId = (value: string) => {
    setAppId_(value);
    setBotTyped(null);
  };
  const typed = [privateKey, webhookSecret, token].some((value) => value.trim() !== "");
  const changed = stored?.mode
    ? current.mode !== stored.mode || current.appId.trim() !== stored.app_id || botEdited || typed
    : current.appId.trim() !== "" || botEdited || typed;
  const body = (): GitHubConnectionIn => {
    const out: GitHubConnectionIn = { mode: current.mode };
    if (current.mode === "app") {
      if (current.appId.trim()) out.app_id = current.appId.trim();
      if (privateKey.trim()) out.private_key = privateKey.trim();
    } else if (token.trim()) {
      out.token = token.trim();
    }
    if (webhookSecret.trim()) out.webhook_secret = webhookSecret.trim();
    if (botEdited && current.bot.trim()) out.bot_login = current.bot.trim();
    return out;
  };
  const clearSecrets = () => {
    setPrivateKey("");
    setWebhookSecret("");
    setToken("");
  };
  return {
    stored,
    ...current,
    changed,
    body,
    clearSecrets,
    setMode,
    setAppId,
    setBot: setBotTyped,
    privateKey,
    setPrivateKey,
    webhookSecret,
    setWebhookSecret,
    token,
    setToken,
  };
}

function GitHubConnectionFields({
  draft,
  canManage,
  row,
}: {
  draft: ReturnType<typeof useConnectionDraft>;
  canManage: boolean;
  row: ReadinessItem | undefined;
}) {
  const token = draft.mode === "token";
  const stored = draft.stored;
  const installed = Object.entries(stored?.installations ?? {});
  return (
    <>
      <div className={FIELD_WIDTH.name}>
        <Select
          label="Connect with"
          options={GITHUB_MODES}
          value={draft.mode}
          disabled={!canManage}
          onChange={(event) => draft.setMode(event.target.value === "token" ? "token" : "app")}
        />
      </div>
      {token ? (
        <p className={cx(INTRO, FIELD_WIDTH.prose)}>{TOKEN_CONSEQUENCES}</p>
      ) : (
        <div className={FIELD_WIDTH.name}>
          <TextField
            label="App ID"
            readOnly={!canManage}
            value={draft.appId}
            onChange={(event) => draft.setAppId(event.target.value)}
            isMonospaced
          />
        </div>
      )}
      {!token && installed.length > 0 && (
        <div data-installations>
          <h4 className="text-body-sm font-medium text-default">Installed on</h4>
          <ul className="text-body-sm text-subtle">
            {installed.map(([owner, id]) => (
              <li key={owner}>
                {owner} · installation {id}
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className={token ? FIELD_WIDTH.repository : FIELD_WIDTH.prose}>
        {canManage &&
          (token ? (
            <TextField
              label="Access token"
              type="password"
              value={draft.token}
              onChange={(event) => draft.setToken(event.target.value)}
              helperMessage={`Leave blank to keep the stored token. ${TOKEN_PERMISSIONS}`}
            />
          ) : (
            <TextArea
              label="Private key"
              value={draft.privateKey}
              onChange={(event) => draft.setPrivateKey(event.target.value)}
              placeholder="Paste the PEM, from -----BEGIN RSA PRIVATE KEY----- on"
              isMonospaced
              minimumRows={4}
              helperMessage="Leave blank to keep the stored key."
            />
          ))}
        <StoredState
          label={token ? "Access token" : "Private key"}
          set={token ? stored?.token_set : stored?.private_key_set}
        />
      </div>
      <div className={FIELD_WIDTH.repository}>
        {canManage && (
          <TextField
            label="Webhook secret"
            type="password"
            value={draft.webhookSecret}
            onChange={(event) => draft.setWebhookSecret(event.target.value)}
            helperMessage="Leave blank to keep the stored secret."
          />
        )}
        <StoredState label="Webhook secret" set={stored?.webhook_secret_set} />
      </div>
      <div className={FIELD_WIDTH.name}>
        <TextField
          label="Bot username"
          readOnly={!canManage || !token}
          value={draft.bot}
          onChange={(event) => draft.setBot(event.target.value)}
          placeholder="factory-bot[bot]"
          helperMessage={
            token ? "Found from GitHub when left blank." : "Read from the GitHub App when saved."
          }
        />
      </div>
      {stored?.webhook_url && (
        <div className={cx(FIELD_WIDTH.repository, "flex items-end gap-2")}>
          <div className="min-w-0 flex-1">
            <TextField
              label="Webhook URL"
              readOnly
              value={stored.webhook_url}
              isMonospaced
              helperMessage="Where GitHub delivers this project's webhooks."
            />
          </div>
          <CopyButton value={stored.webhook_url} name="Webhook URL" spacing="default" />
        </div>
      )}
      {row && row.state !== "ok" && <ConnectionNote row={row} />}
    </>
  );
}

/** Whether a write-only value is stored: the API says so and never returns the value. */
function StoredState({ label, set }: { label: string; set: boolean | undefined }) {
  if (set === undefined) return null;
  return (
    <p data-stored-state className="pt-1 text-body-sm text-subtle">
      {label}: {set ? "Stored" : "Not set"}
    </p>
  );
}

/** A connection or webhook row that needs saying, under the fields it is about. */
function ConnectionNote({ row }: { row: ReadinessItem }) {
  return (
    <div className="pt-1">
      <RowState row={row} />
      {row.detail && row.state !== "unknown" && (
        <p className="pt-1 text-body-sm text-default">
          <ServerText text={row.detail} />
        </p>
      )}
    </div>
  );
}

function TicketSourceForm({
  project,
  connection: connectionRow,
  webhook,
}: {
  project: ProjectDetail;
  connection?: ReadinessItem;
  webhook?: ReadinessItem;
}) {
  const put = usePutTicketSource(project.id);
  const update = useUpdateProject(project.id);
  const putSecret = usePutSecret(project.id);
  const connect = usePutGitHubConnection(project.id);
  const draft = useConnectionDraft(project.id);
  const canManage = useCan("projects:manage");
  const source = project.ticket_source;
  // The connection route is the only writer of `settings.github`, so the form never sends it back.
  const { github: _github, ...settings } = project.settings ?? {};
  const stored = (settings.gitlab ?? {}) as Record<string, unknown>;
  const [tracker, setTracker] = useState<"github" | "gitlab">(
    source?.kind === "gitlab" || settings.forge === "gitlab" ? "gitlab" : "github",
  );
  const gitlab = tracker === "gitlab";
  const [issues, setIssues] = useState(source?.external_ref ?? project.github_repo);
  const [gitlabUrl, setGitlabUrl] = useState(String(stored.base_url ?? ""));
  const [gitlabToken, setGitlabToken] = useState("");
  const [gitlabWebhook, setGitlabWebhook] = useState("");
  const [gitlabBot, setGitlabBot] = useState(String(stored.bot_login ?? ""));
  // Only a single-organization deployment reads a CA file path from a project.
  const caPathAllowed = !useMultiOrg();
  const [gitlabCaPath, setGitlabCaPath] = useState(String(stored.ca_bundle ?? ""));
  const [gitlabCa, setGitlabCa] = useState("");
  const issuesProblem = repoProblemOf(issues, tracker);
  const urlProblem =
    gitlab && !/^https?:\/\//.test(gitlabUrl.trim())
      ? "Enter the GitLab URL, including https://."
      : null;
  const gitlabSettings = {
    base_url: gitlabUrl.trim().replace(/\/$/, ""),
    bot_login: gitlabBot.trim(),
    ...(caPathAllowed ? { ca_bundle: gitlabCaPath.trim() } : {}),
  };
  // No source yet counts as a change: the field starts on the project's repository, and saving
  // it as it stands is how most projects set one. A typed secret is a change too: a blank one
  // keeps what is stored.
  const changed =
    repoName(issues) !== (source?.external_ref ?? null) ||
    tracker !== (source?.kind ?? "github") ||
    (gitlab &&
      (Object.entries(gitlabSettings).some(([key, value]) => value !== String(stored[key] ?? "")) ||
        [gitlabToken, gitlabWebhook, gitlabCa].some((value) => value.trim() !== ""))) ||
    (!gitlab && draft.changed);
  const polled = !source
    ? canManage
      ? "Not set yet: save to set it."
      : "Not set yet."
    : source.last_polled_at
      ? `Last checked for new issues ${formatRelative(source.last_polled_at)}.`
      : "Not checked for new issues yet.";
  return (
    // `mutate`, not `mutateAsync`: the failure is rendered from the mutation's `error`, and
    // `mutateAsync` also rejects — with nothing awaiting it, an unhandled rejection.
    <Form
      onSubmit={() => {
        if (!canManage) return;
        // The connection saves after the ticket source: it looks up an installation for each
        // repository, and refuses when they moved while it did.
        const connection = !gitlab && draft.changed ? draft.body() : null;
        put.mutate(
          {
            kind: tracker,
            external_ref: repoName(issues),
            webhook_secret_ref: gitlab
              ? "GITLAB_WEBHOOK_SECRET"
              : (source?.webhook_secret_ref ?? null),
          },
          {
            onSuccess: () => {
              if (connection) connect.mutate(connection, { onSuccess: draft.clearSecrets });
            },
          },
        );
        if (!gitlab) return;
        update.mutate({ settings: { ...settings, forge: "gitlab", gitlab: gitlabSettings } });
        if (gitlabToken.trim()) {
          putSecret.mutate({ name: "GITLAB_TOKEN", value: gitlabToken.trim() });
        }
        if (gitlabWebhook.trim()) {
          putSecret.mutate({ name: "GITLAB_WEBHOOK_SECRET", value: gitlabWebhook.trim() });
        }
        if (gitlabCa.trim()) {
          putSecret.mutate({ name: "GITLAB_CA_BUNDLE", value: gitlabCa.trim() });
        }
      }}
      validate={() => !issuesProblem && !urlProblem}
      className="space-y-4"
    >
      {({ attempted }) => (
        <>
          <div className={FIELD_WIDTH.name}>
            <Select
              label="Source"
              options={TICKET_TRACKERS}
              value={tracker}
              disabled={!canManage}
              onChange={(event) =>
                setTracker(event.target.value === "gitlab" ? "gitlab" : "github")
              }
              helperMessage="GitHub connects with an App or an access token stored on this project. GitLab uses a token stored on this project."
            />
          </div>
          {gitlab && (
            <div className={FIELD_WIDTH.repository}>
              <TextField
                label="GitLab URL"
                isRequired
                readOnly={!canManage}
                value={gitlabUrl}
                onChange={(event) => setGitlabUrl(event.target.value)}
                placeholder="https://gitlab.example.com"
                isMonospaced
                errorMessage={attempted && urlProblem ? urlProblem : undefined}
              />
            </div>
          )}
          <div className={FIELD_WIDTH.repository}>
            <TextField
              label={gitlab ? "GitLab project" : "Issues repository"}
              isRequired
              readOnly={!canManage}
              value={issues}
              onChange={(event) => setIssues(event.target.value)}
              placeholder={gitlab ? "group/app" : "https://github.com/acme/widgets"}
              isMonospaced
              helperMessage={
                gitlab
                  ? `Path with namespace. Issues and merge requests come from here. ${polled}`
                  : `The repository whose issues become tickets. ${polled}`
              }
              errorMessage={
                (attempted || issues.trim() !== "") && issuesProblem ? issuesProblem : undefined
              }
            />
          </div>
          {!gitlab && (
            <GitHubConnectionFields draft={draft} canManage={canManage} row={connectionRow} />
          )}
          {!gitlab && webhook && webhook.state !== "ok" && webhook.state !== "unknown" && (
            <ConnectionNote row={webhook} />
          )}
          {!gitlab && webhook?.summary === "not needed" && webhook.detail && (
            <p className="pt-1 text-body-sm text-default">
              <ServerText text={webhook.detail} />
            </p>
          )}
          {gitlab && (
            <>
              {canManage && (
                <>
                  <div className={FIELD_WIDTH.repository}>
                    <TextField
                      label="Access token"
                      type="password"
                      value={gitlabToken}
                      onChange={(event) => setGitlabToken(event.target.value)}
                      helperMessage="Leave blank to keep the stored token. A group or project access token."
                    />
                  </div>
                  <div className={FIELD_WIDTH.repository}>
                    <TextField
                      label="Webhook secret"
                      type="password"
                      value={gitlabWebhook}
                      onChange={(event) => setGitlabWebhook(event.target.value)}
                      helperMessage="Leave blank to keep the stored secret."
                    />
                  </div>
                </>
              )}
              <div className={FIELD_WIDTH.name}>
                <TextField
                  label="Bot username"
                  readOnly={!canManage}
                  value={gitlabBot}
                  onChange={(event) => setGitlabBot(event.target.value)}
                  placeholder="factory-bot"
                />
              </div>
              {caPathAllowed && (
                <div className={FIELD_WIDTH.repository}>
                  <TextField
                    label="CA bundle path"
                    readOnly={!canManage}
                    value={gitlabCaPath}
                    onChange={(event) => setGitlabCaPath(event.target.value)}
                    placeholder="/etc/ssl/certs/org-ca.pem"
                    isMonospaced
                    helperMessage="Optional PEM file on this host. Any private CA."
                  />
                </div>
              )}
              {canManage && (
                <div className={FIELD_WIDTH.prose}>
                  <TextArea
                    label="CA bundle PEM"
                    value={gitlabCa}
                    onChange={(event) => setGitlabCa(event.target.value)}
                    placeholder="Paste the PEM, from -----BEGIN CERTIFICATE----- on"
                    isMonospaced
                    minimumRows={4}
                    helperMessage="Optional. Paste extra CA certificates. Stored as GITLAB_CA_BUNDLE. Leave blank to keep the stored bundle."
                  />
                </div>
              )}
            </>
          )}
          {canManage && (
            <>
              <ErrorMessage
                error={put.error ?? connect.error ?? update.error ?? putSecret.error}
                title="Couldn't set the ticket source"
              />
              <FormFooter>
                <Button
                  type="submit"
                  appearance="primary"
                  isDisabled={!changed}
                  isLoading={
                    put.isPending || connect.isPending || update.isPending || putSecret.isPending
                  }
                >
                  {source ? "Update source" : "Set source"}
                </Button>
              </FormFooter>
            </>
          )}
        </>
      )}
    </Form>
  );
}

/** One requirement of a page that holds several: its heading, its state on a line of its own
 *  under it, then its body. */
function Subsection({
  title,
  row,
  children,
}: {
  title: string;
  row?: ReadinessItem | null;
  children: ReactNode;
}) {
  return (
    <section className="space-y-3 p-4">
      <div className="space-y-1">
        <h3 className="text-heading-sm text-default">{title}</h3>
        {row && <RowState row={row} />}
      </div>
      {children}
    </section>
  );
}

/** A row's one-line state under its heading. A row nobody could check reads as information —
 *  "not checked: Factorial cannot reach …" is an explanation, not a defect — and says why when the
 *  server said, since "not checked" alone sends nobody anywhere. */
function RowState({ row }: { row: ReadinessItem }) {
  const readiness = readinessOf(row.key, row.state);
  const ink = row.state === "unknown" ? ROLE_ICON.information.ink : readiness.ink;
  const glyph = row.state === "unknown" ? ROLE_ICON.information.glyph : readiness.glyph;
  return (
    <p className="flex items-start gap-1.5 text-body-sm text-subtle">
      <Icon glyph={glyph} label={readiness.label} size="small" className={cx("mt-0.5", ink)} />
      <span className="min-w-0">
        <ServerText text={row.state === "unknown" && row.detail ? row.detail : row.summary} />
      </span>
    </p>
  );
}

export function RuntimeBody({ project }: { project: ProjectDetail }) {
  const verdicts = new Map(
    project.readiness.filter((row) => row.key === "runtime").map((row) => [row.process, row]),
  );
  const canManage = useCan("projects:manage");
  const canRuntimes = useCan("runtimes:manage");
  const runtimes = useRuntimes({ enabled: canRuntimes });
  const permits = usePermitChange(project.id);
  const allowed = listSetting(project.settings, RUNTIME_ALLOWLIST);
  // An E2B runtime this project does not permit: every spawn naming it is refused until an
  // operator allows it, which is one click here rather than a trip through the API.
  const needsPermit = (slug: string | null) =>
    canManage && slug !== null && unpermitted([slug], runtimes.data ?? [], allowed).length > 0;
  return (
    <div className="space-y-3 p-4">
      <p className={INTRO}>
        Each process picks a runtime in its process file (<Code>runtime:</Code>). To run, the
        runtime must exist and be signed in. A cloud sandbox runtime must also be permitted here. A
        runtime problem stops only the processes that use it.
      </p>
      <ul className="divide-y divide-default rounded-md border border-default">
        {project.processes.map(({ process_key }) => {
          const verdict = verdicts.get(process_key);
          return (
            <li key={process_key} className="space-y-1 px-3 py-2">
              <div className="flex items-center justify-between gap-3">
                <Code>{process_key}</Code>
                {verdict ? (
                  <span className="inline-flex items-center gap-1 text-body-sm">
                    <Icon
                      glyph={verdict.state === "ok" ? "status-success" : "status-error"}
                      label={verdict.state === "ok" ? "Ready" : "Cannot run"}
                      className={verdict.state === "ok" ? "text-icon-success" : "text-icon-danger"}
                    />
                    {verdict.runtime_slug}
                  </span>
                ) : (
                  <span className="text-body-sm text-subtle">None</span>
                )}
              </div>
              {verdict?.detail && (
                <p className="text-body-sm text-subtle">
                  <ServerText text={verdict.detail} />
                </p>
              )}
              {verdict && needsPermit(verdict.runtime_slug) && (
                <div className="flex justify-end">
                  <Button
                    isLoading={permits.isPending}
                    onClick={() =>
                      permits.apply(
                        { runtime_allowlist: { add: [verdict.runtime_slug ?? ""] } },
                        `Permitted ${verdict.runtime_slug}`,
                        `Process ${verdict.process} can now run on it.`,
                      )
                    }
                  >
                    Permit {verdict.runtime_slug} on this project
                  </Button>
                </div>
              )}
            </li>
          );
        })}
      </ul>
      <ErrorMessage error={permits.error} />
      {canRuntimes && (
        <div className="flex justify-end">
          <LinkButton to="/runtimes">Manage runtimes</LinkButton>
        </div>
      )}
    </div>
  );
}

/** The project's name and description. */
export function ProjectDetailsBody({ project }: { project: ProjectDetail }) {
  const update = useUpdateProject(project.id);
  const canManage = useCan("projects:manage");
  const [name, setName] = useState(project.name);
  const [description, setDescription] = useState(project.description ?? "");
  const nameProblem = name.trim() === "" ? "A project needs a name." : null;
  const changes = {
    ...(name.trim() !== project.name && { name: name.trim() }),
    // Empty clears it rather than storing "", so the list renders no subtitle at all.
    ...(description.trim() !== (project.description ?? "") && {
      description: description.trim() || null,
    }),
  };
  const changed = Object.keys(changes).length > 0;

  return (
    <Form
      onSubmit={() => canManage && update.mutate(changes)}
      validate={() => !nameProblem}
      className="space-y-4 p-4"
    >
      {({ attempted }) => (
        <>
          <div className={FIELD_WIDTH.name}>
            <TextField
              label="Name"
              isRequired
              readOnly={!canManage}
              value={name}
              onChange={(event) => setName(event.target.value)}
              errorMessage={attempted ? (nameProblem ?? undefined) : undefined}
            />
          </div>
          <div className={FIELD_WIDTH.prose}>
            <TextArea
              label="Description"
              readOnly={!canManage}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder={canManage ? "What this project builds, for whom" : undefined}
            />
          </div>
          {canManage && (
            <>
              <ErrorMessage error={update.error} title="Couldn't save the details" />
              <FormFooter>
                <Button
                  type="submit"
                  appearance="primary"
                  isDisabled={!changed}
                  isLoading={update.isPending}
                >
                  Save details
                </Button>
              </FormFooter>
            </>
          )}
        </>
      )}
    </Form>
  );
}
