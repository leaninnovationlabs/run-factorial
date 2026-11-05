import { describe, expect, it } from "vitest";
import { commitHref, forgeHost, issueHref, pullHref, pullName, repoHref, treeHref } from "./forge";

const GITLAB = forgeHost({ kind: "gitlab" }, { gitlab: { base_url: "https://gitlab.example" } });

describe("forgeHost", () => {
  it("stays on GitHub when the project has no GitLab source", () => {
    expect(forgeHost(null, {}).kind).toBe("github");
    expect(forgeHost(null, {}).origin).toBe("https://github.com");
    expect(forgeHost({ kind: "github" }, { gitlab: { base_url: "https://gitlab.com" } }).kind).toBe(
      "github",
    );
  });

  it("uses the project's GitLab origin, and gitlab.com when none is stored", () => {
    expect(GITLAB.origin).toBe("https://gitlab.example");
    expect(forgeHost({ kind: "gitlab" }, {}).origin).toBe("https://gitlab.com");
  });

  it("prefers the API display payload over the ticket-source fallback", () => {
    const display = {
      kind: "github",
      origin: "https://github.example",
      name: "GitHub",
      pull_noun: "PR",
      issue_path: "/issues/{key}",
      pull_path: "/pull/{number}",
      tree_path: "/tree/{branch}",
      commit_path: "/commit/{sha}",
    };
    expect(forgeHost({ kind: "gitlab" }, {}, display).origin).toBe("https://github.example");
    expect(issueHref("acme/app", "12", display)).toBe("https://github.example/acme/app/issues/12");
  });
});

describe("forge URLs", () => {
  it("builds the GitHub paths the dashboard already linked", () => {
    expect(issueHref("acme/app", "12")).toBe("https://github.com/acme/app/issues/12");
    expect(pullHref("acme/app", "7")).toBe("https://github.com/acme/app/pull/7");
    expect(repoHref("acme/app")).toBe("https://github.com/acme/app");
  });

  it("builds the GitLab paths for the same objects", () => {
    expect(issueHref("group/app", "12", GITLAB)).toBe(
      "https://gitlab.example/group/app/-/issues/12",
    );
    expect(pullHref("group/app", "7", GITLAB)).toBe(
      "https://gitlab.example/group/app/-/merge_requests/7",
    );
    expect(treeHref("group/app", "spec/12-x", GITLAB)).toBe(
      "https://gitlab.example/group/app/-/tree/spec/12-x",
    );
    expect(commitHref("group/app", "abc", GITLAB)).toBe(
      "https://gitlab.example/group/app/-/commit/abc",
    );
    expect(repoHref("group/app", GITLAB)).toBe("https://gitlab.example/group/app");
  });
});

describe("pullName", () => {
  it("names the request each forge reviews, in words", () => {
    expect(pullName()).toBe("pull request");
    expect(pullName(GITLAB)).toBe("merge request");
  });
});
