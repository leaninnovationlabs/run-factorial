// @vitest-environment happy-dom
// @vitest-environment-options {"settings":{"disableIframePageLoading":true}}
/**
 * A file a step declares, in the preview drawer (decisions 58 and 71): framed with scripts,
 * same-origin and forms off, kept steady through polls, and explained when there is none to
 * frame.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../../api/client";
import type { ChangedFile, OutputEntry, StepRun } from "../../api/types";
import { AuthProvider } from "../../auth";
import { type ForgeHost, forgeHost } from "../../forge";
import { run } from "../../pages/TicketPage.fixture";
import { byText, hover, type Mounted, mount, settle } from "../../testDom";
import { FilePane } from "./FilePane";

let view: Mounted | null = null;

beforeEach(() => {
  vi.spyOn(api, "authMe").mockResolvedValue({ email: "operator@example.com" });
});

afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.restoreAllMocks();
});

const PUBLISHED: OutputEntry = {
  kind: "file",
  key: "specs/{ticket}-{slug}/summary.html",
  label: "Summary",
  status: "published",
  path: "specs/12-widget/summary.html",
  commit: "c".repeat(40),
  size: 2048,
  too_large: false,
  matches: [],
  reason: null,
  url: "/pages/7?blob=b&expires=1&signature=s",
  github_url: "https://github.com/acme/app/blob/ccc/specs/12-widget/summary.html",
  changed: true,
  group: null,
  group_total: null,
  group_url: null,
};
const plan = run(7, "plan", 1, "succeeded") as StepRun;
const frame = () => document.querySelector("iframe");
const pane = (
  file: OutputEntry,
  shown: StepRun = plan,
  changed: ChangedFile[] = [],
  forge?: ForgeHost,
) => (
  <AuthProvider>
    <FilePane file={file} run={shown} step="plan" changed={changed} forge={forge} />
  </AuthProvider>
);
const CHANGED: ChangedFile[] = [
  { path: "out/report.htm", github_url: "https://github.com/acme/widgets/blob/c/out/report.htm" },
  { path: "notes.txt", github_url: "https://github.com/acme/widgets/blob/c/notes.txt" },
];
const MISSING: OutputEntry = { ...PUBLISHED, status: "missing", url: null, path: null };
const listed = () =>
  [...document.querySelectorAll("li a")].map((link) => ({
    text: link.textContent,
    href: link.getAttribute("href"),
    target: link.getAttribute("target"),
    rel: link.getAttribute("rel"),
  }));
const loading = () =>
  [...document.querySelectorAll('[role="status"]')].map((status) => status.textContent);

describe("the file pane", () => {
  it("frames a published file with scripts, same-origin and forms off", async () => {
    view = await mount(pane(PUBLISHED));
    await settle();

    expect(frame()?.getAttribute("sandbox")).toBe("allow-popups allow-popups-to-escape-sandbox");
    expect(frame()?.getAttribute("src")).toMatch(/\/pages\/7\?blob=b&expires=1&signature=s$/);
    expect(frame()?.getAttribute("referrerpolicy")).toBe("no-referrer");
    expect(document.body.textContent).toContain("specs/12-widget/summary.html at ccccccc");
    expect(byText(document.body, "a", "GitHub")?.getAttribute("href")).toBe(PUBLISHED.github_url);
  });

  it("names a markdown file by its path and frames it like any other", async () => {
    view = await mount(
      pane({
        ...PUBLISHED,
        path: "specs/12-widget/spec.md",
        github_url: "https://github.com/acme/app/blob/ccc/specs/12-widget/spec.md",
      }),
    );
    await settle();

    expect(document.body.textContent).toContain("specs/12-widget/spec.md at ccccccc");
    expect(frame()?.getAttribute("sandbox")).toBe("allow-popups allow-popups-to-escape-sandbox");
  });

  it("keeps the link it mounted with through a poll that brings a fresh one", async () => {
    view = await mount(pane(PUBLISHED));
    await settle();
    const first = frame()?.getAttribute("src");

    await view.rerender(pane({ ...PUBLISHED, url: "/pages/7?blob=b&expires=2&signature=t" }));
    await settle();

    expect(frame()?.getAttribute("src")).toBe(first);
  });

  it("opens the newest link in a new tab while the frame keeps the one it mounted with", async () => {
    view = await mount(pane(PUBLISHED));
    await settle();
    const first = frame()?.getAttribute("src");

    await view.rerender(pane({ ...PUBLISHED, url: "/pages/7?blob=b&expires=2&signature=t" }));
    await settle();

    expect(byText(document.body, "a", "New tab")?.getAttribute("href")).toMatch(
      /\/pages\/7\?blob=b&expires=2&signature=t$/,
    );
    expect(frame()?.getAttribute("src")).toBe(first);
  });

  it.each([
    [
      { status: "pending", url: null },
      "Attempt 1 has finished. Its file appears here in a moment.",
    ],
    [{ status: "missing", path: null, url: null }, "File not found"],
    [
      { status: "ambiguous", matches: ["specs/12-a/spec.md", "specs/12-b/spec.md"], url: null },
      "The path matches 2 files: specs/12-a/spec.md, specs/12-b/spec.md. Make the path in the process config more specific.",
    ],
    [
      { status: "lookup_failed", reason: "GitHub is down", url: null },
      "Couldn't load this file from GitHubGitHub is down.",
    ],
    [{ too_large: true, url: null }, "over 1 MB"],
    [{ too_large: false, url: null }, "has no link"],
  ] as [Partial<OutputEntry>, string][])("explains %o without a frame", async (patch, text) => {
    view = await mount(pane({ ...PUBLISHED, ...patch }));
    await settle();

    expect(frame()).toBeNull();
    expect(document.body.textContent).toContain(text);
  });

  it("says a running attempt's file comes when it ends, and shows nothing loading", async () => {
    const going = run(7, "plan", 1, "running") as StepRun;
    view = await mount(pane({ ...PUBLISHED, status: "pending", url: null }, going));
    await settle();

    expect(document.body.textContent).toContain(
      "Not ready yetThis file appears here when attempt 1 finishes.",
    );
    expect(loading()).toEqual([]);
  });

  it("shows the file loading from GitHub once the attempt has ended", async () => {
    view = await mount(pane({ ...PUBLISHED, status: "pending", url: null }));
    await settle();

    expect(loading()).toEqual(["Loading the file…"]);
  });

  it("shows the frame loading from GitHub until it has, and again on Reload", async () => {
    view = await mount(pane(PUBLISHED));
    await settle();
    expect(loading()).toEqual(["Loading the file from GitHub…"]);

    frame()?.dispatchEvent(new Event("load"));
    await settle();
    expect(loading()).toEqual([]);

    byText(document.body, "button", "Reload")?.click();
    await settle();
    expect(loading()).toEqual(["Loading the file from GitHub…"]);
  });

  it("keeps a published file's size out of the explanation when its link is what is missing", async () => {
    view = await mount(pane({ ...PUBLISHED, url: null }));
    await settle();

    expect(document.body.textContent).not.toContain("over 1 MB");
    expect(document.body.textContent).toContain(
      "This file can't be shown here. Open it on GitHub.",
    );
  });

  it("names a GitLab project's host on its link, its loading and its explanations", async () => {
    const gitlab = forgeHost({ kind: "gitlab" }, {});
    view = await mount(pane(PUBLISHED, plan, [], gitlab));
    await settle();
    expect(byText(document.body, "a", "GitLab")?.getAttribute("href")).toBe(PUBLISHED.github_url);
    expect(loading()).toEqual(["Loading the file from GitLab…"]);
    await view.unmount();

    view = await mount(pane({ ...PUBLISHED, url: null }, plan, [], gitlab));
    await settle();
    expect(document.body.textContent).toContain(
      "This file can't be shown here. Open it on GitLab.",
    );
    await view.unmount();

    view = await mount(
      pane({ ...PUBLISHED, status: "lookup_failed", reason: null, url: null }, plan, [], gitlab),
    );
    await settle();
    expect(document.body.textContent).toContain("Couldn't load this file from GitLab");
    expect(document.body.textContent).not.toContain("GitHub");
  });

  it("draws Reload, New tab and GitHub as small outlined buttons", async () => {
    view = await mount(pane(PUBLISHED));
    await settle();

    const toolbar = [
      byText(document.body, "button", "Reload"),
      byText(document.body, "a", "New tab"),
      byText(document.body, "a", "GitHub"),
    ];
    expect(toolbar.map((control) => control?.dataset.appearance)).toEqual(Array(3).fill("outline"));
    expect(toolbar.map((control) => control?.dataset.spacing)).toEqual(Array(3).fill("small"));
  });

  it("shows the whole path on hover when the toolbar cuts it short", async () => {
    const path = "specs/12-a-long-feature-name-the-toolbar-cannot-fit/spec.md";
    view = await mount(pane({ ...PUBLISHED, path }));
    await settle();
    const name = document.querySelector("span.font-mono") as HTMLElement;
    Object.defineProperty(name, "scrollWidth", { configurable: true, value: 400 });
    Object.defineProperty(name, "clientWidth", { configurable: true, value: 120 });

    await hover(name);
    await settle(400);

    expect(document.querySelector('[role="tooltip"]')?.textContent).toBe(`${path} at ccccccc`);
  });

  it("leaves the file's name off the toolbar unless it was published or is known missing", async () => {
    view = await mount(pane({ ...PUBLISHED, status: "pending", url: null }));
    await settle();
    expect(document.body.textContent).not.toContain("File not found");

    await view.rerender(pane({ ...PUBLISHED, status: "missing", url: null }));
    await settle();
    expect(document.querySelectorAll("span.font-mono")[0]?.textContent).toBe("File not found");
  });

  it("lists the files the step changed under a missing file, each linked to GitHub", async () => {
    view = await mount(pane(MISSING, plan, CHANGED));
    await settle();

    expect(document.body.textContent).toContain("Files this step changed (2)");
    expect(document.body.textContent).toContain(
      "If one of these is the file you expected, fix its path in the step's preview settings.",
    );
    expect(listed()).toEqual(
      CHANGED.map((file) => ({
        text: file.path,
        href: file.github_url,
        target: "_blank",
        rel: "noreferrer",
      })),
    );
  });

  it("lists nothing for a missing file when the step changed nothing", async () => {
    view = await mount(pane(MISSING, plan, []));
    await settle();

    expect(document.body.textContent).not.toContain("Files this step changed");
  });

  it("lists nothing beside a published file", async () => {
    view = await mount(pane(PUBLISHED, plan, CHANGED));
    await settle();

    expect(document.body.textContent).not.toContain("Files this step changed");
  });

  it("shows a path as text, never as markup", async () => {
    const path = "<img src=x onerror=alert(1)>.md";
    view = await mount(
      pane(MISSING, plan, [{ path, github_url: "https://github.com/a/b/blob/c/x" }]),
    );
    await settle();

    expect(listed().map((link) => link.text)).toEqual([path]);
    expect(document.querySelector("img")).toBeNull();
  });

  it("shows a long path whole in the list, file name included", async () => {
    const path = "specs/12-long-slug/reports/components/button/accessibility.html";
    view = await mount(
      pane(MISSING, plan, [{ path, github_url: "https://github.com/a/b/blob/c/x" }]),
    );
    await settle();

    const item = document.querySelector("li");
    expect(item?.className).not.toContain("truncate");
    expect(item?.className).toContain("break-all");
  });
});
