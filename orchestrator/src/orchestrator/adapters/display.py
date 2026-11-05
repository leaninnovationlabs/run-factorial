"""Forge display metadata and readiness copy. The API sends this so the UI does not branch."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final
from urllib.parse import quote

GITHUB_NOT_CONNECTED = (
    "This project has no GitHub connection. Connect it with an App or a token on Ticket source."
)
GITLAB_NOT_CONFIGURED = (
    "GitLab is not configured for this project. Set settings.gitlab.base_url and save GITLAB_TOKEN."
)

__all__ = ["GITHUB_DISPLAY", "GITLAB_DISPLAY", "HostDisplay", "display_for", "protection_command"]


@dataclass(frozen=True, slots=True)
class HostDisplay:
    """What the API and readiness need to talk about a code host without naming it."""

    kind: str
    name: str
    origin: str
    pull_noun: str
    github_app: bool
    unconfigured: str
    labels_hand: str
    config_rules_cannot_write: str
    no_ci: str
    workflows_unconfigured: str
    gate_approvers_fix: str
    protection_remedy: Callable[[str, str, frozenset[str]], str]
    config_protection_remedy: Callable[[str], str]
    config_rules_shared: str
    issue_path: str
    pull_path: str
    tree_path: str
    commit_path: str

    def issue_href(self, repo: str, key: str) -> str:
        return f"{self.origin}/{repo}{self.issue_path.format(key=key)}"

    def pull_href(self, repo: str, number: str) -> str:
        return f"{self.origin}/{repo}{self.pull_path.format(number=number)}"

    def tree_href(self, repo: str, branch: str) -> str:
        return f"{self.origin}/{repo}{self.tree_path.format(branch=branch)}"

    def commit_href(self, repo: str, sha: str) -> str:
        return f"{self.origin}/{repo}{self.commit_path.format(sha=sha)}"

    def payload(self) -> dict[str, str]:
        """What the API sends so the UI does not branch on the forge kind."""
        return {
            "kind": self.kind,
            "name": self.name,
            "origin": self.origin,
            "pull_noun": self.pull_noun,
            "issue_path": self.issue_path,
            "pull_path": self.pull_path,
            "tree_path": self.tree_path,
            "commit_path": self.commit_path,
        }


#: Applied by the operator, never by the orchestrator — see `api/readiness.py`'s
#: `protection_item`. Mirrors the profile CLAUDE.md fixes: one approval, CODEOWNERS, `verify`,
#: dismiss-stale, no App bypass.
_PROTECTION_REMEDY: Final = (
    "gh api -X PUT repos/{repo}/branches/{branch}/protection --input - <<'JSON'\n"
    '{{"required_status_checks":{{"strict":true,"contexts":{contexts}}},'
    '"enforce_admins":true,'
    '"required_pull_request_reviews":{{"required_approving_review_count":1,'
    '"require_code_owner_reviews":true,"dismiss_stale_reviews":true}},'
    '"restrictions":null}}\nJSON'
)


def protection_command(repo: str, branch: str, checks: frozenset[str] = frozenset()) -> str:
    """The PUT that applies the whole profile. It replaces the rule, so it carries the required
    checks the branch already has: `verify` joins them rather than replacing them."""
    contexts = ",".join(f'"{name}"' for name in sorted(checks | {"verify"}))
    return _PROTECTION_REMEDY.format(repo=repo, branch=branch, contexts=f"[{contexts}]")


#: The App holds no `administration: write` (design.md §7), so it cannot write this ruleset. The
#: bypass is the repository admin role, never the App: it is what makes the reset possible.
_CONFIG_RULESET_REMEDY: Final = (
    "gh api -X POST repos/{repo}/rulesets --input - <<'JSON'\n"
    '{{"name":"factorial-config","target":"branch","enforcement":"active",'
    '"conditions":{{"ref_name":{{"include":["refs/heads/factorial"],"exclude":[]}}}},'
    '"rules":[{{"type":"deletion"}},{{"type":"non_fast_forward"}}],'
    '"bypass_actors":[{{"actor_id":5,"actor_type":"RepositoryRole","bypass_mode":"always"}}]}}'
    "\nJSON"
)


def _github_config_protection(repo: str) -> str:
    return _CONFIG_RULESET_REMEDY.format(repo=repo)


def _gitlab_config_protection(repo: str) -> str:
    project = quote(repo, safe="")
    return (
        f"glab api --method POST projects/{project}/protected_branches"
        " -f name=factorial -f push_access_level=30 -f merge_access_level=30"
        " -f allow_force_push=false"
    )


def _gitlab_protection(repo: str, branch: str, checks: frozenset[str]) -> str:
    del checks
    project = quote(repo, safe="")
    return (
        f"glab api --method PUT projects/{project}"
        " -f only_allow_merge_if_pipeline_succeeds=true\n"
        f"glab api --method POST projects/{project}/protected_branches"
        f" -f name={branch} -f allow_force_push=false"
        " -f code_owner_approval_required=true\n"
        f"glab api --method POST projects/{project}/approvals"
        " -f approvals_before_merge=1 -f reset_approvals_on_push=true"
    )


GITHUB_DISPLAY = HostDisplay(
    kind="github",
    name="GitHub",
    origin="https://github.com",
    pull_noun="PR",
    github_app=True,
    unconfigured=GITHUB_NOT_CONNECTED,
    labels_hand="Create them by hand, as the connect docs describe.",
    config_rules_cannot_write="The App cannot write rulesets, so run the command below.",
    no_ci="defines nothing under .github/workflows on its default branch. ",
    workflows_unconfigured=f"Workflows not checked. {GITHUB_NOT_CONNECTED} ",
    gate_approvers_fix=(
        "List those reviewers in CODEOWNERS, or remove approverPermission from the gate."
    ),
    protection_remedy=protection_command,
    config_protection_remedy=_github_config_protection,
    config_rules_shared=(
        " The command adds the factorial-config ruleset, which blocks force-push and "
        "deletion together and lets a repository admin bypass it to reset the branch."
    ),
    issue_path="/issues/{key}",
    pull_path="/pull/{number}",
    tree_path="/tree/{branch}",
    commit_path="/commit/{sha}",
)


GITLAB_DISPLAY = HostDisplay(
    kind="gitlab",
    name="GitLab",
    origin="https://gitlab.com",
    pull_noun="MR",
    github_app=False,
    unconfigured=GITLAB_NOT_CONFIGURED,
    labels_hand="Create the labels by hand.",
    config_rules_cannot_write="Factorial only reads branch protection, so run the command below.",
    no_ci="has no CI config file (default .gitlab-ci.yml) on its default branch. ",
    workflows_unconfigured=f"Workflows not checked. {GITLAB_NOT_CONFIGURED} ",
    gate_approvers_fix=(
        "List those reviewers as code owners, or remove approverPermission from the gate."
    ),
    protection_remedy=_gitlab_protection,
    config_protection_remedy=_gitlab_config_protection,
    config_rules_shared=(
        " The command protects the branch, which blocks force-push and deletion together and "
        "leaves Developers able to push, so the builder's commits still land."
    ),
    issue_path="/-/issues/{key}",
    pull_path="/-/merge_requests/{number}",
    tree_path="/-/tree/{branch}",
    commit_path="/-/commit/{sha}",
)


_DISPLAY = {"github": GITHUB_DISPLAY, "gitlab": GITLAB_DISPLAY}


def display_for(kind: str | None, *, origin: str | None = None) -> HostDisplay:
    """Display metadata for a forge kind. Unknown or missing kinds are GitHub."""
    base = _DISPLAY.get(kind or "github", GITHUB_DISPLAY)
    if origin and origin != base.origin:
        return HostDisplay(
            kind=base.kind,
            name=base.name,
            origin=origin.rstrip("/"),
            pull_noun=base.pull_noun,
            github_app=base.github_app,
            unconfigured=base.unconfigured,
            labels_hand=base.labels_hand,
            config_rules_cannot_write=base.config_rules_cannot_write,
            no_ci=base.no_ci,
            workflows_unconfigured=base.workflows_unconfigured,
            gate_approvers_fix=base.gate_approvers_fix,
            protection_remedy=base.protection_remedy,
            config_protection_remedy=base.config_protection_remedy,
            config_rules_shared=base.config_rules_shared,
            issue_path=base.issue_path,
            pull_path=base.pull_path,
            tree_path=base.tree_path,
            commit_path=base.commit_path,
        )
    return base
