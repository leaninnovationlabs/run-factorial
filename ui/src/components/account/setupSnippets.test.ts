import { execFileSync } from "node:child_process";
import { describe, expect, it } from "vitest";
import { claudeSnippet, codexSnippet, shellSingleQuote } from "./setupSnippets";

const MCP_URL = "https://app.example.com/mcp/";

/** What a POSIX shell makes of `word`, with the token set, so an expansion would show. */
function asShellReads(word: string): string {
  return execFileSync("/bin/sh", ["-c", `printf '%s' ${word}`], {
    encoding: "utf8",
    env: { FACTORIAL_MCP_TOKEN: "leaked-token" },
  });
}

describe("setup snippets", () => {
  it("quotes any text as one shell word that reads back unchanged", () => {
    for (const text of ["a'b", "''", '$FACTORIAL_MCP_TOKEN `echo hi` \\ "x"']) {
      expect(asShellReads(shellSingleQuote(text))).toBe(text);
    }
  });

  it("gives Claude Code the variable's name, never its value", () => {
    const prefix = "claude mcp add-json --scope user factorial ";
    const command = claudeSnippet(MCP_URL);
    expect(command.startsWith(prefix)).toBe(true);
    const word = command.slice(prefix.length);
    expect(word.startsWith("'") && word.endsWith("'")).toBe(true);
    expect(JSON.parse(asShellReads(word))).toEqual({
      type: "http",
      url: MCP_URL,
      headers: { Authorization: `Bearer \${FACTORIAL_MCP_TOKEN}` },
    });
  });

  it("has Codex read the token from the environment by name", () => {
    expect(codexSnippet(MCP_URL)).toBe(
      "codex mcp add factorial --url 'https://app.example.com/mcp/' --bearer-token-env-var FACTORIAL_MCP_TOKEN",
    );
  });
});
