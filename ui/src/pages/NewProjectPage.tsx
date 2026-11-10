import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { useMultiOrg } from "../api/organizationQueries";
import { useCreateProject } from "../api/queries";
import {
  Button,
  ErrorMessage,
  Form,
  FormFooter,
  LinkButton,
  PageHeader,
  Panel,
  RequiredLegend,
  Select,
  TextArea,
  TextField,
  usePageTitle,
} from "../components/ui";

const REPO_PATTERN = /^[^/\s]+(?:\/[^/\s]+)+$/;

const FORGE_OPTIONS = [
  { value: "github", label: "GitHub" },
  { value: "gitlab", label: "GitLab" },
];

/**
 * Step one of configuring a project: name it and point it at a repo.
 *
 * Only the two things a project cannot exist without. The ticket source, config sync, secrets
 * and activation all live on the project page, because each of them can fail on its own (a repo
 * with no `.factorial/`, a `process.yaml` that does not parse) and each needs its own error next to
 * its own button — a five-step wizard that dead-ends on step four with a half-made project is
 * worse than a page you can come back to.
 */
export function NewProjectPage() {
  const navigate = useNavigate();
  const create = useCreateProject();
  const [forge, setForge] = useState<"github" | "gitlab">("github");
  const [name, setName] = useState("");
  const [repo, setRepo] = useState("");
  const [description, setDescription] = useState("");
  const [gitlabUrl, setGitlabUrl] = useState("");
  const [gitlabToken, setGitlabToken] = useState("");
  const [gitlabWebhook, setGitlabWebhook] = useState("");
  const [gitlabBot, setGitlabBot] = useState("");
  const [gitlabCa, setGitlabCa] = useState("");
  const [gitlabCaPath, setGitlabCaPath] = useState("");
  // Only a single-organization deployment reads a CA file path from a project.
  const caPathAllowed = !useMultiOrg();
  const [followOnError, setFollowOnError] = useState<unknown>(null);
  usePageTitle(["Create project"]);

  const nameProblem = name.trim() === "" ? "Give the project a name." : null;
  const repoProblem = !REPO_PATTERN.test(repo.trim())
    ? forge === "gitlab"
      ? "Use group/project, including nested groups."
      : "Use owner/name."
    : null;
  const urlProblem =
    forge === "gitlab" && !/^https?:\/\//.test(gitlabUrl.trim())
      ? "Enter the GitLab URL, including https://."
      : null;
  const tokenProblem =
    forge === "gitlab" && gitlabToken.trim() === "" ? "Paste a GitLab access token." : null;

  return (
    <div className="space-y-6">
      <PageHeader title="Create project" />

      <Panel>
        <Form
          validate={() => !nameProblem && !repoProblem && !urlProblem && !tokenProblem}
          onSubmit={() => {
            setFollowOnError(null);
            create.mutate(
              {
                name: name.trim(),
                github_repo: repo.trim(),
                description: description.trim() || null,
                settings:
                  forge === "gitlab"
                    ? {
                        forge: "gitlab",
                        gitlab: {
                          base_url: gitlabUrl.trim().replace(/\/$/, ""),
                          bot_login: gitlabBot.trim(),
                          ...(caPathAllowed ? { ca_bundle: gitlabCaPath.trim() } : {}),
                        },
                      }
                    : {},
              },
              {
                onSuccess: (project) => {
                  if (forge !== "gitlab") {
                    navigate(`/projects/${project.id}`);
                    return;
                  }
                  void (async () => {
                    try {
                      await api.putTicketSource(project.id, {
                        kind: "gitlab",
                        external_ref: repo.trim(),
                        webhook_secret_ref: gitlabWebhook.trim() ? "GITLAB_WEBHOOK_SECRET" : null,
                      });
                      await api.putSecret(project.id, "GITLAB_TOKEN", {
                        value: gitlabToken.trim(),
                      });
                      if (gitlabWebhook.trim()) {
                        await api.putSecret(project.id, "GITLAB_WEBHOOK_SECRET", {
                          value: gitlabWebhook.trim(),
                        });
                      }
                      if (gitlabCa.trim()) {
                        await api.putSecret(project.id, "GITLAB_CA_BUNDLE", {
                          value: gitlabCa.trim(),
                        });
                      }
                      navigate(`/projects/${project.id}`);
                    } catch (error) {
                      setFollowOnError(error);
                    }
                  })();
                },
              },
            );
          }}
          className="space-y-4 p-4"
        >
          {({ attempted }) => (
            <>
              <RequiredLegend />
              <TextField
                label="Name"
                isRequired
                value={name}
                autoFocus
                onChange={(event) => setName(event.target.value)}
                placeholder="widgets"
                helperMessage="Must be unique in this organization."
                errorMessage={attempted ? (nameProblem ?? undefined) : undefined}
              />

              <Select
                label="Issue and code source"
                isRequired
                value={forge}
                onChange={(event) =>
                  setForge(event.target.value === "gitlab" ? "gitlab" : "github")
                }
                options={FORGE_OPTIONS}
                helperMessage="GitHub uses the GitHub App. GitLab uses the token you paste below."
              />

              {forge === "github" ? (
                <TextField
                  label="GitHub repository"
                  isRequired
                  value={repo}
                  onChange={(event) => setRepo(event.target.value)}
                  placeholder="acme/widgets"
                  isMonospaced
                  helperMessage="The repository that holds the code and the .factorial/ config. Install the GitHub App on it first."
                  errorMessage={
                    (attempted || repo.trim() !== "") && repoProblem ? repoProblem : undefined
                  }
                />
              ) : (
                <>
                  <TextField
                    label="GitLab URL"
                    isRequired
                    value={gitlabUrl}
                    onChange={(event) => setGitlabUrl(event.target.value)}
                    placeholder="https://gitlab.example.com"
                    isMonospaced
                    helperMessage="Your self-hosted GitLab, including https://."
                    errorMessage={attempted ? (urlProblem ?? undefined) : undefined}
                  />
                  <TextField
                    label="GitLab project"
                    isRequired
                    value={repo}
                    onChange={(event) => setRepo(event.target.value)}
                    placeholder="group/app"
                    isMonospaced
                    helperMessage="Path with namespace, as group/project or group/sub/project. Issues and merge requests come from here."
                    errorMessage={
                      (attempted || repo.trim() !== "") && repoProblem ? repoProblem : undefined
                    }
                  />
                  <TextField
                    label="Access token"
                    isRequired
                    type="password"
                    value={gitlabToken}
                    onChange={(event) => setGitlabToken(event.target.value)}
                    helperMessage="A group or project access token with the api, read_repository and write_repository scopes. Factorial stores it as a project secret."
                    errorMessage={attempted ? (tokenProblem ?? undefined) : undefined}
                  />
                  <TextField
                    label="Webhook secret"
                    type="password"
                    value={gitlabWebhook}
                    onChange={(event) => setGitlabWebhook(event.target.value)}
                    helperMessage="Optional. Use the same secret as the GitLab webhook that posts to /webhooks/gitlab."
                  />
                  <TextField
                    label="Bot username"
                    value={gitlabBot}
                    onChange={(event) => setGitlabBot(event.target.value)}
                    placeholder="factory-bot"
                    helperMessage="Optional. The GitLab username that owns the token. Factorial uses it to ignore its own comments."
                  />
                  {caPathAllowed && (
                    <TextField
                      label="CA bundle path"
                      value={gitlabCaPath}
                      onChange={(event) => setGitlabCaPath(event.target.value)}
                      placeholder="/etc/ssl/certs/org-ca.pem"
                      isMonospaced
                      helperMessage="Optional. The path to a PEM file on the host that runs Factorial. Any private CA works."
                    />
                  )}
                  <TextArea
                    label="CA bundle PEM"
                    value={gitlabCa}
                    onChange={(event) => setGitlabCa(event.target.value)}
                    placeholder="Paste the PEM, starting with -----BEGIN CERTIFICATE-----"
                    isMonospaced
                    minimumRows={4}
                    helperMessage="Optional. Paste extra CA certificates. Factorial stores them as the project secret GITLAB_CA_BUNDLE."
                  />
                </>
              )}

              <TextField
                label="Description"
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                placeholder="Optional"
              />

              <ErrorMessage error={create.error ?? followOnError} />

              <FormFooter align="end">
                <LinkButton to="/projects">Cancel</LinkButton>
                <Button type="submit" appearance="primary" isLoading={create.isPending}>
                  Create project
                </Button>
              </FormFooter>
            </>
          )}
        </Form>
      </Panel>
    </div>
  );
}
