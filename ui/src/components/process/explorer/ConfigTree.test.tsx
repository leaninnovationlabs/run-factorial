// @vitest-environment happy-dom
/**
 * `ConfigTree` renders `tree.ts`'s output and nothing else — no internal state of its own.
 * Folding and tri-state selection are exercised by rendering twice with different props; anything
 * that needed real interaction belongs in `tree.ts` instead, which carries its own coverage. The
 * one exception is a manifest's warning, which lives in an `InlineMessage` that only opens on a
 * press, so that test mounts (decision 51).
 */

import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { ConfigFile, FileProposal } from "../../../api/types";
import { click, mount } from "../../../testDom";
import { ConfigTree } from "./ConfigTree";
import type { ExplorerFile } from "./tree";

const proposal = (path: string, over: Partial<FileProposal> = {}): FileProposal => ({
  path,
  body: "body",
  component: "plan-agent",
  kind: "agent",
  variant: "base-target",
  version: 1,
  facts: {},
  present_in_repo: false,
  ...over,
});

/** A path the commit would write. */
const file = (path: string, over: Partial<FileProposal> = {}): ExplorerFile => ({
  path,
  proposal: proposal(path, over),
});

/** A path the pinned commit already has — edited in place in the editor, and the only kind that
 *  carries a warning. */
const repoFile = (path: string, over: Partial<ConfigFile> = {}): ExplorerFile => ({
  path,
  repo: { path, kind: "agent", drift: "same", warning: null, sha: "b".repeat(40), ...over },
});

const MANIFEST = ".factorial/agents/plan-agent/agent.yaml";
const PROMPT = ".factorial/agents/plan-agent/system-prompt.md";
const FILES = [file(MANIFEST), file(PROMPT)];
const ALL_OPEN = new Set([".factorial", ".factorial/agents", ".factorial/agents/plan-agent"]);
const noop = () => {};

const tree = (over: Partial<Parameters<typeof ConfigTree>[0]> = {}) => (
  <ConfigTree
    files={FILES}
    expanded={ALL_OPEN}
    selected={new Set<string>()}
    edited={new Set<string>()}
    stale={new Set<string>()}
    openPath={null}
    onToggle={noop}
    onExpand={noop}
    onOpen={noop}
    {...over}
  />
);

const draw = (over: Partial<Parameters<typeof ConfigTree>[0]> = {}) => renderToString(tree(over));

describe("ConfigTree", () => {
  it("renders a file row when its directory is expanded", () => {
    expect(draw()).toContain("agent.yaml");
  });

  it("hides a directory's files when it is folded shut", () => {
    expect(draw({ expanded: new Set([".factorial"]) })).not.toContain("agent.yaml");
  });

  it("marks a directory mixed when only some of its files are selected", () => {
    expect(draw({ selected: new Set([MANIFEST]) })).toContain('data-state="partial"');
  });

  it("marks a directory checked when all of its files are selected", () => {
    const html = draw({ selected: new Set([MANIFEST, PROMPT]) });
    // The checkbox's name is the label text beside it, hidden from sight but not from a reader.
    expect(html).toMatch(
      /<input[^>]*data-state="on"[^>]*\/><span class="sr-only">Include every file in/,
    );
    expect(html).not.toContain('data-state="partial"');
  });

  it("draws no lock on a repo file the editor can edit", () => {
    const html = draw({ files: [repoFile(MANIFEST)] });
    expect(html).toContain("In repo");
    expect(html).not.toContain('data-glyph="lock"');
    expect(html).not.toContain("checkbox");
  });

  it("locks a repo file where it cannot be edited", () => {
    // The read-only drawer edits nothing; in the editor, the pin and the head disagree on it.
    expect(draw({ files: [repoFile(MANIFEST)], onToggle: undefined })).toContain(
      'data-glyph="lock"',
    );
    expect(draw({ files: [repoFile(MANIFEST, { drift: "differs" })] })).toContain(
      'data-glyph="lock"',
    );
  });

  it("badges an edited file and a stale one distinctly", () => {
    // Edited is a note; stale is a warning with the one thing to do about it — open the file and
    // confirm it — so the two can never be read as the same mark.
    const html = draw({ edited: new Set([MANIFEST, PROMPT]), stale: new Set([PROMPT]) });
    expect(html.match(/>Edited</g)).toHaveLength(2);
    expect(html.match(/data-appearance="warning"[^>]*>(?:<[^>]+>)*Review</g)).toHaveLength(1);
  });

  it("flags an unticked agent manifest as a stub, since assemble.py writes one over it", () => {
    // Selecting the manifest is what stops the server from replacing it with a comments-only
    // stub (`config/scaffold/assemble.py`) — a fact that must come from the operator's own
    // selection, never from `offered`/`default_on`, which only says what the catalog could offer.
    const html = draw({ selected: new Set() });
    expect(html).toMatch(/>\s*Empty template\s*</);
  });

  it("does not flag a ticked manifest as a stub", () => {
    expect(draw({ selected: new Set([MANIFEST]) })).not.toMatch(/>\s*Empty template\s*</);
  });

  it("never flags a locked manifest as a stub, since the repo's own file is left untouched", () => {
    const html = draw({ files: [file(MANIFEST, { present_in_repo: true })], selected: new Set() });
    expect(html).not.toMatch(/>\s*Empty template\s*</);
  });

  // --- what the retired `agents` readiness row used to say, per file ---------------------------

  it("hangs a manifest's warning on the manifest, as a mark named for what it is", () => {
    // A warning glyph on the manifest's own row, a button a keyboard can reach — never a
    // native `title`, which shows to a mouse alone.
    const html = draw({
      files: [repoFile(MANIFEST, { warning: "plan-agent: empty document" })],
    });
    expect(html).toMatch(
      /aria-label="Unusable: agent.yaml"[^>]*>(?:<[^>]+>)*<svg[^>]*status-warning/,
    );
    expect(html).not.toContain("title=");
  });

  it("says what is wrong with the manifest when its mark is pressed", async () => {
    const view = await mount(
      tree({ files: [repoFile(MANIFEST, { warning: "plan-agent: empty document" })] }),
    );
    expect(document.body.textContent).not.toContain("plan-agent: empty document");
    await click(view.container.querySelector('[aria-label="Unusable: agent.yaml"]'));
    const opened = document.querySelector('[role="dialog"]');
    expect(opened?.textContent).toContain("plan-agent: empty document");
    await view.unmount();
  });

  it("says nothing about a manifest that resolves", () => {
    const html = draw({ files: [repoFile(MANIFEST)] });
    expect(html).not.toContain("Unusable");
    expect(html).not.toContain('data-glyph="status-warning"');
  });

  it("marks a file the repo's head has moved since the pin", () => {
    const html = draw({ files: [repoFile(MANIFEST, { drift: "differs" })] });
    expect(html).toContain("Changed on branch");
  });

  it("says nothing about drift when the file matches, or when the head could not be read", () => {
    for (const drift of ["same", "unknown"] as const) {
      const html = draw({ files: [repoFile(MANIFEST, { drift })] });
      expect(html).not.toContain("Changed on branch");
      expect(html).not.toContain('data-glyph="status-warning"');
    }
  });

  it("offers no tick for a repo file, which reaches a commit as an edit instead", () => {
    // A tick picks the catalog's body, and `assemble.py` refuses a pick over any path the repo
    // owns, so a tick on one would be a submission the server is bound to reject.
    expect(draw({ files: [repoFile(MANIFEST)] })).not.toContain("checkbox");
    expect(draw({ files: [file(MANIFEST)] })).toContain("checkbox");
  });

  it("marks which file the right pane is showing", () => {
    // Said to a reader with `aria-current`, and drawn as the selected ground — on that row alone.
    const html = draw({ openPath: MANIFEST });
    expect(html.match(/aria-current="true"/g)).toHaveLength(1);
    expect(html).toMatch(/aria-current="true"[^>]*>(?:<[^>]+>)*agent\.yaml</);
    expect(html.match(/\bbg-selected\b/g)).toHaveLength(1);
    expect(draw({ openPath: null })).not.toContain("aria-current");
  });

  // --- what the commit will do, in ink --------------------------------------------------

  it("inks a file the commit would create in the success role, up through the folders that hold it", () => {
    const html = draw({ selected: new Set([MANIFEST]) });
    expect(html.match(/\btext-success\b/g)?.length).toBeGreaterThan(1);
  });

  it("inks a repo file the commit would rewrite in the information role", () => {
    const PROCESS = ".factorial/process.yaml";
    const html = draw({
      files: [repoFile(PROCESS, { kind: "process" })],
      modified: new Set([PROCESS]),
    });
    expect(html).toMatch(/\btext-information\b/);
    expect(html).not.toMatch(/\btext-success\b/);
  });

  it("leaves an untouched row in the ordinary ink", () => {
    const html = draw({ files: [repoFile(MANIFEST)], selected: new Set() });
    expect(html).not.toMatch(/\btext-success\b/);
    expect(html).not.toMatch(/\btext-information\b/);
  });
});
