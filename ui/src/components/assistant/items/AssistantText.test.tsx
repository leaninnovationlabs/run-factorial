// @vitest-environment happy-dom
/** The assistant's markdown: GFM, and nothing a model's text could use to run or fetch. */

import { afterEach, describe, expect, test } from "vitest";
import { byText, click, type Mounted } from "../../../testDom";
import { mountInAssistant } from "../testFakes";
import { AssistantText } from "./AssistantText";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
});

async function render(text: string) {
  const mounted = await mountInAssistant(<AssistantText text={text} />);
  view = mounted.view;
  return mounted;
}

describe("AssistantText", () => {
  test("AssistantText renders a GFM table and drops raw HTML", async () => {
    const { view } = await render(
      [
        "| Ticket | Step |",
        "| --- | --- |",
        "| acme/api#212 | build |",
        "",
        '<script>alert("x")</script> <b>bold</b> <img src="x" onerror="alert(1)">',
        "",
        "~~gone~~ and a [ ] task",
      ].join("\n"),
    );
    const root = view.container;
    expect([...root.querySelectorAll("th")].map((cell) => cell.textContent)).toEqual([
      "Ticket",
      "Step",
    ]);
    expect([...root.querySelectorAll("td")].map((cell) => cell.textContent)).toEqual([
      "acme/api#212",
      "build",
    ]);
    expect(root.querySelector("script, b, img, [onerror]")).toBeNull();
    expect(root.textContent).not.toContain("alert");
    expect(root.querySelector("del")?.textContent).toBe("gone");
  });

  test("AssistantText keeps only http(s) and dashboard links", async () => {
    const { view, where } = await render(
      [
        "[docs](https://example.com/guide)",
        "[ticket](/tickets/212)",
        "[script](javascript:alert(1))",
        "[data](data:text/html,hi)",
        "[mail](mailto:operator@example.com)",
        "[elsewhere](//evil.example/path)",
        "[relative](tickets/3)",
      ].join(" "),
    );
    const root = view.container;
    const links = [...root.querySelectorAll("a")];
    expect(links.map((link) => [link.textContent, link.getAttribute("href")])).toEqual([
      ["docs", "https://example.com/guide"],
      ["ticket", "/tickets/212"],
    ]);
    expect(links[0]?.getAttribute("target")).toBe("_blank");
    expect(links[0]?.getAttribute("rel")).toContain("noreferrer");
    // Not blue: a link keeps the text's colour and says what it is with an underline.
    for (const link of links) expect(link.className).toContain("underline");
    for (const word of ["script", "data", "mail", "elsewhere", "relative"]) {
      expect(root.textContent).toContain(word);
    }
    await click(byText(root, "a", "ticket"));
    expect(where()).toBe("/tickets/212");
  });

  test("a line that starts with a bullet character is a list item", async () => {
    const { view } = await render("Blocked tickets:\n• one\n• two\n  ◦ nested");
    const items = [...view.container.querySelectorAll("li")];
    expect(items.map((item) => item.firstChild?.textContent?.trim())).toEqual([
      "one",
      "two",
      "nested",
    ]);
    expect(view.container.querySelector("p")?.textContent).toBe("Blocked tickets:");
  });

  test("a bullet character inside a code fence stays as written", async () => {
    const { view } = await render("```\n• keep me\n```");
    expect(view.container.querySelector("li")).toBeNull();
    expect(view.container.querySelector("pre")?.textContent).toContain("• keep me");
  });

  test("a numbered list keeps its number after a code block", async () => {
    const { view } = await render("1. first\n\n```\ncode\n```\n\n2. second\n3. third");
    const lists = [...view.container.querySelectorAll("ol")];
    expect(lists).toHaveLength(2);
    expect(lists[1]?.getAttribute("start")).toBe("2");
  });

  test("a task item shows its checkbox and no disc", async () => {
    const { view } = await render("- [ ] todo\n- [x] done");
    const items = [...view.container.querySelectorAll("li")];
    expect(items).toHaveLength(2);
    for (const item of items) {
      expect(item.querySelector('input[type="checkbox"]')).not.toBeNull();
      expect(item.className).toContain("list-none");
    }
  });

  test("fenced code is a code block the reader can copy", async () => {
    const { view } = await render("```yaml\nname: simple\nruntime: claude_cli\n```");
    const block = view.container.querySelector("figure");
    expect(block?.querySelector("pre")?.textContent).toBe("name: simple\nruntime: claude_cli");
    expect(block?.querySelector("button")?.getAttribute("aria-label")).toContain("Copy");
  });

  test("AssistantText never loads an image, whatever its address", async () => {
    const { view } = await render("![the chart](https://evil.example/p.png?secret=1)");
    expect(view.container.querySelector("img")).toBeNull();
    expect(view.container.textContent).toContain("the chart");
  });
});
