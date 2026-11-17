/** The code editor's field says what it is, and which file, to a script that reads a line of it. */

import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { CodeEditor } from "./CodeEditor";

describe("CodeEditor", () => {
  it("marks its field as a code editor, with the file when it is one", () => {
    const html = renderToString(
      <CodeEditor label="process.yaml" value="kind: Process" file=".factorial/process.yaml" />,
    );
    expect(html).toMatch(/<textarea[^>]* data-code-editor="true"/);
    expect(html).toContain('data-code-file=".factorial/process.yaml"');
  });

  it("names no file when it holds none", () => {
    const html = renderToString(<CodeEditor label="Draft" value="" />);
    expect(html).toMatch(/<textarea[^>]* data-code-editor="true"/);
    expect(html).not.toContain("data-code-file");
  });
});
