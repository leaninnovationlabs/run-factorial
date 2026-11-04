/**
 * What the MCP access page tells a person to paste, per client. No snippet carries the token: each
 * names the variable, and the client reads it from the shell it starts in. A token written into a
 * config file or a profile would sit under `HOME`, which every host-runtime agent inherits.
 */

export const TOKEN_VAR = "FACTORIAL_MCP_TOKEN";

/** One POSIX shell word. Nothing inside single quotes expands; a quote in the text closes the
 *  word, adds an escaped quote and opens it again. */
export function shellSingleQuote(s: string): string {
  return `'${s.replaceAll("'", "'\\''")}'`;
}

function claudeEntry(url: string) {
  return { type: "http", url, headers: { Authorization: `Bearer \${${TOKEN_VAR}}` } };
}

/** Single-quoted, so `${FACTORIAL_MCP_TOKEN}` reaches Claude Code as written and it expands the
 *  variable at each start. */
export function claudeSnippet(url: string): string {
  const entry = shellSingleQuote(JSON.stringify(claudeEntry(url)));
  return `claude mcp add-json --scope user factorial ${entry}`;
}

/** The same entry as a project's `.mcp.json`. */
export function claudeProjectFile(url: string): string {
  return JSON.stringify({ mcpServers: { factorial: claudeEntry(url) } }, null, 2);
}

export function codexSnippet(url: string): string {
  return `codex mcp add factorial --url ${shellSingleQuote(url)} --bearer-token-env-var ${TOKEN_VAR}`;
}

/** The same entry in `~/.codex/config.toml`. */
export function codexConfigFile(url: string): string {
  return [
    "[mcp_servers.factorial]",
    `url = ${JSON.stringify(url)}`,
    `bearer_token_env_var = "${TOKEN_VAR}"`,
  ].join("\n");
}
