/**
 * Server-sent events, parsed from the text a `fetch` body yields. The Drawer reads its stream with
 * `fetch` rather than `EventSource`, which cannot send the bearer header a token sign-in needs
 * (tech-stack decision 63), so it parses the events itself.
 *
 * A chunk ends wherever the network cut it: `carry` is the unfinished message, handed back to be
 * prefixed to the next chunk. A comment (`: ping`, the server's heartbeat) is a line with no
 * field name, so it carries no event.
 */

export type SseMessage = { id?: string; event: string; data: string };

export function parseSse(chunk: string, carry: string): { messages: SseMessage[]; carry: string } {
  const blocks = (carry + chunk).replace(/\r\n/g, "\n").split("\n\n");
  const rest = blocks.pop() ?? "";
  const messages: SseMessage[] = [];
  for (const block of blocks) {
    let id: string | undefined;
    let event = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      const raw = colon === -1 ? "" : line.slice(colon + 1);
      const value = raw.startsWith(" ") ? raw.slice(1) : raw;
      if (field === "data") data.push(value);
      else if (field === "event") event = value;
      else if (field === "id") id = value;
    }
    if (data.length === 0) continue;
    messages.push(
      id === undefined ? { event, data: data.join("\n") } : { id, event, data: data.join("\n") },
    );
  }
  return { messages, carry: rest };
}
