// @vitest-environment happy-dom
/** What a right-click pointed at, as the assistant is told of it: short, capped, and never a throw. */

import { afterEach, describe, expect, test } from "vitest";
import { describeTarget, keepsNativeMenu } from "./pointed";

afterEach(() => {
  document.body.innerHTML = "";
  window.getSelection()?.removeAllRanges();
});

function page(html: string): HTMLElement {
  document.body.innerHTML = html;
  return document.body;
}

const at = (selector: string) => document.querySelector(selector) as HTMLElement;

describe("describeTarget", () => {
  test("a cell of a table row is the row: its cells, its link and its page", () => {
    page(`<table><tbody><tr>
      <td>#212</td><td><a href="/tickets/212">Checkout total ignores the discount code</a></td>
      <td>Blocked</td><td id="cell">build</td>
    </tr></tbody></table>`);
    expect(describeTarget(at("#cell"), "/projects/5/tickets")).toEqual({
      label: "#212 · Checkout total ignores the discount code · Blocked · build",
      text: "#212 · Checkout total ignores the discount code · Blocked · build",
      page: "/projects/5/tickets",
      href: "/tickets/212",
      anchor: null,
      file: null,
      lines: null,
    });
  });

  test("a cell that stacks two things reads as both, a space apart", () => {
    page(`<table><tbody><tr>
      <td id="cell"><div><span>#</span><span>804</span><button aria-label="Open"></button></div><div>Sign in with SSO</div></td>
      <td><code>build</code></td>
    </tr></tbody></table>`);
    expect(describeTarget(at("#cell"), "/projects/5/tickets").label).toBe(
      "#804 Sign in with SSO · build",
    );
  });

  test("a control with a name the assistant knows carries the name", () => {
    page(
      `<section data-assist-anchor="ticket.actions"><h2>Actions</h2><button id="b">Restart</button></section>`,
    );
    const pointed = describeTarget(at("#b"), "/tickets/7");
    expect(pointed.label).toBe("Restart");
    expect(pointed.anchor).toBe("ticket.actions");
  });

  test("selected text that holds the target is what was pointed at", () => {
    page(`<p id="p">The test step failed twice on a timeout.</p>`);
    const range = document.createRange();
    range.selectNodeContents(at("#p"));
    window.getSelection()?.addRange(range);
    const pointed = describeTarget(at("#p"), "/tickets/7");
    expect(pointed.label).toBe('"The test step failed twice on a timeout."');
    expect(pointed.text).toBe("The test step failed twice on a timeout.");
  });

  test("a selection the caller does not vouch for is passed over for the thing itself", () => {
    page(
      `<table><tbody><tr><td id="cell">Checkout total</td><td>Blocked</td></tr></tbody></table>`,
    );
    const range = document.createRange();
    range.selectNodeContents(at("#cell"));
    window.getSelection()?.addRange(range);
    expect(describeTarget(at("#cell"), "/overview", false).label).toBe("Checkout total · Blocked");
  });

  test("in the config editor it is the file and the selected lines, or the caret's line", () => {
    page(
      `<textarea data-code-editor data-code-file=".factorial/processes/process-simple.yaml"></textarea>`,
    );
    const editor = at("textarea") as HTMLTextAreaElement;
    editor.value = "kind: Process\nname: simple\nsteps:\n  - name: spec\n  - name: build";
    editor.setSelectionRange(14, 34);
    expect(describeTarget(editor, "/projects/5/configuration/process")).toEqual({
      label: "process-simple.yaml, lines 2 to 3",
      text: "name: simple\nsteps:",
      page: "/projects/5/configuration/process",
      href: null,
      anchor: null,
      file: ".factorial/processes/process-simple.yaml",
      lines: [2, 3],
    });
    editor.setSelectionRange(0, 0);
    const caret = describeTarget(editor, "/projects/5/configuration/process");
    expect([caret.label, caret.text, caret.lines]).toEqual([
      "process-simple.yaml, line 1",
      "kind: Process",
      [1, 1],
    ]);
  });

  test("an element with nothing to read is the page", () => {
    page(`<main><div id="gap"></div></main>`);
    expect(describeTarget(at("#gap"), "/overview")).toMatchObject({
      label: "This page",
      text: "",
      page: "/overview",
    });
  });

  test("a wall of text is capped: 80 characters to name it, 2,000 to tell it", () => {
    page(`<p id="p">${"word ".repeat(1000)}</p>`);
    const pointed = describeTarget(at("#p"), "/overview");
    expect(pointed.label.length).toBe(80);
    expect(pointed.label.endsWith("…")).toBe(true);
    expect(pointed.text.length).toBe(2000);
  });

  test("a link is read as the router addresses it, without the base the browser shows", () => {
    page(`<table><tbody><tr>
      <td id="cell"><a href="/factorial/orgs/acme/tickets/212">Checkout total</a></td>
    </tr><tr>
      <td id="other"><a href="/factorial/orgs/another/tickets/9">Elsewhere</a></td>
    </tr></tbody></table>`);
    const base = "/factorial/orgs/acme";
    expect(describeTarget(at("#cell"), "/overview", true, base).href).toBe("/tickets/212");
    // Another organization's page is not a page of this dashboard.
    expect(describeTarget(at("#other"), "/overview", true, base).href).toBeNull();
  });

  test("a link out of the dashboard is not a place the assistant can go", () => {
    page(`<p><a id="a" href="https://github.com/acme/api/issues/212">acme/api#212</a></p>`);
    expect(describeTarget(at("#a"), "/overview").href).toBeNull();
  });
});

describe("keepsNativeMenu", () => {
  const click = (target: Element, shiftKey = false, button = 2) => {
    // A right-click: the keyboard's menu key sends button 0, with Shift when it is Shift+F10.
    const event = new MouseEvent("contextmenu", { bubbles: true, shiftKey, button });
    Object.defineProperty(event, "target", { value: target });
    return event;
  };

  test("Shift keeps the browser's menu anywhere", () => {
    page(`<p id="p">words</p>`);
    expect(keepsNativeMenu(click(at("#p")))).toBe(false);
    expect(keepsNativeMenu(click(at("#p"), true))).toBe(true);
  });

  test("Shift+F10 is the keyboard asking for the assistant's menu, not for the browser's", () => {
    page(`<button id="b">Restart</button>`);
    expect(keepsNativeMenu(click(at("#b"), true, 0))).toBe(false);
  });

  test("the chat itself, a text field and the terminal keep it; the config editor does not", () => {
    page(`
      <aside data-assistant-mini><p id="mini">a reply</p></aside>
      <aside data-assistant-dock><p id="dock">a reply</p></aside>
      <section data-assistant-chat><p id="chat">a reply</p></section>
      <div role="dialog"><div data-item-stream></div><h2 id="drawer">Factorial Assistant</h2></div>
      <input id="field" /><textarea id="notes"></textarea>
      <div class="xterm"><span id="term">$</span></div>
      <textarea id="editor" data-code-editor></textarea>
      <div role="dialog"><h2 id="other">Output</h2></div>
      <div role="menu"><button id="entry">Add to current conversation</button></div>
      <div role="tooltip"><p id="card">Blocked at build</p></div>
    `);
    for (const id of [
      "mini",
      "dock",
      "chat",
      "drawer",
      "field",
      "notes",
      "term",
      "entry",
      "card",
    ]) {
      expect(keepsNativeMenu(click(at(`#${id}`))), id).toBe(true);
    }
    expect(keepsNativeMenu(click(at("#editor")))).toBe(false);
    expect(keepsNativeMenu(click(at("#other")))).toBe(false);
  });
});
