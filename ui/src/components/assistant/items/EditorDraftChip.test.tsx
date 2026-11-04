// @vitest-environment happy-dom
/** An editor card: the draft opens in the process editor, at a click. */

import { afterEach, describe, expect, test } from "vitest";
import { byText, click, type Mounted } from "../../../testDom";
import { chatItem, mountInAssistant } from "../testFakes";
import { EditorDraftChip } from "./EditorDraftChip";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
});

describe("EditorDraftChip", () => {
  test("EditorDraftChip opens the editor at ?draft=", async () => {
    const item = chatItem(41, 9, "editor_draft", {
      project_id: 3,
      process: "standard",
      content: "name: standard\n",
      files: [],
      edits: [{ path: ".factorial/agents/build/prompt.md", body: "x", base_sha: "b".repeat(40) }],
      base_sha: "a".repeat(40),
      would_write: [
        { path: ".factorial/processes/process-standard.yaml", origin: "process" },
        { path: ".factorial/agents/build/prompt.md", origin: "edit" },
      ],
    });
    const mounted = await mountInAssistant(<EditorDraftChip item={item} />);
    view = mounted.view;
    expect(view.container.textContent).toContain("standard");
    expect(view.container.textContent).toContain("2 files");
    await click(byText(view.container, "button", "Open in the process editor"));
    expect(mounted.where()).toBe("/projects/3/configuration/process?draft=41");
  });
});
