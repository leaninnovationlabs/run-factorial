/**
 * The controls a Show me card may point at: each is the `data-assist-anchor` of one element, on
 * the page the comment above it names. A closed list, pinned to the server's copy
 * (`mcp_server/chat/routes.py`) by `test_assist_anchors_pinned.py`.
 */

export const ASSIST_ANCHORS = [
  // /overview
  "overview.board",
  // /projects/:projectId and the project's other pages: the side navigation inside a project
  "project.nav",
  // /projects/:projectId and /projects/:projectId/configuration
  "project.admission",
  // /projects/:projectId
  "project.spend",
  // /projects/:projectId/configuration
  "configuration.check",
  // /projects/:projectId/configuration/process, inside the process editor
  "process.commit",
  // /tickets/:ticketId
  "ticket.actions",
  "ticket.graph",
  "ticket.activity",
  // /runtimes
  "runtimes.new",
  // /account/mcp
  "mcp.new_token",
  // /settings
  "settings.credential",
] as const;

export type AssistAnchor = (typeof ASSIST_ANCHORS)[number];

export function isAssistAnchor(name: unknown): name is AssistAnchor {
  return typeof name === "string" && (ASSIST_ANCHORS as readonly string[]).includes(name);
}

export function anchorSelector(name: AssistAnchor): string {
  return `[data-assist-anchor="${name}"]`;
}
