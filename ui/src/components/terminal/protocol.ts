/**
 * The terminal WebSocket's wire format (decision 57), mirroring the gateway's
 * `orchestrator/terminal/protocol.py`.
 *
 * `/factorial/terminal/runs/{step_run_id}?cols=&rows=`. Every viewer attaches read-only; a run has
 * one writer seat, and only its holder's keystrokes reach the session. The dashboard always
 * connects with the session cookie: nobody signs in to it with the API token.
 *
 * Binary frames carry bytes both ways. Text frames are JSON: in, `resize`, `claim` (with `force` to
 * take the seat from its holder) and `release`; out, `hello` once after attach, `status` when the
 * run's status, the counts, who is watching or the seat change, `seat` answering a claim or telling
 * a holder it lost the seat, and `exit` before a 1000 close. Keys sent before a claim's answer
 * says `yours` are dropped by the gateway, never held for after it.
 */

import { APP_PREFIX } from "../../prefix";

export const CloseCode = {
  ENDED: 1000,
  /** Never sent by the gateway: the browser's code for a connection that dropped or never
   *  opened. A refused handshake (foreign Origin, no credential) arrives as this. */
  ABNORMAL: 1006,
  MALFORMED: 1008,
  INTERNAL: 1011,
  RESTARTING: 1012,
  SIGNED_OUT: 4401,
  FORBIDDEN: 4403,
  NO_TERMINAL: 4404,
  TOO_SLOW: 4408,
  PARKED: 4409,
  CROWDED: 4429,
  UNREACHABLE: 4502,
} as const;

/** `holder` is the credential's name (an email, or `api-token`); `yours` is true while this
 *  viewer's keystrokes reach the session. */
export type Seat = { holder: string | null; yours: boolean };

/** `names`: who is watching, a name per viewer (so one person in two tabs twice), sorted. Empty
 *  from a gateway that sends none. */
export type Hello = {
  type: "hello";
  status: string | null;
  target: "session" | "inspect";
  viewers: number;
  writers: number;
  seat: Seat;
  names: string[];
};

export type Status = {
  type: "status";
  status: string | null;
  viewers: number;
  writers: number;
  seat: Seat;
  names: string[];
};

/** `taken`: someone took the seat from this viewer. `reason`: why a claim was refused, or why the
 *  seat freed without its holder releasing it. */
export type SeatFrame = { type: "seat"; taken: boolean; reason: string | null } & Seat;

export type Exit = { type: "exit"; reason: string };

export type ServerFrame = Hello | Status | SeatFrame | Exit;

const MAX_SIDE = 0xffff;

function isCount(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

function namesOf(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((name) => typeof name === "string") : [];
}

function seatOf(value: unknown): Seat {
  const seat =
    typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
  return {
    holder: typeof seat.holder === "string" ? seat.holder : null,
    yours: seat.yours === true,
  };
}

/** A text frame from the gateway, or null for anything this client does not understand. */
export function parseServerFrame(text: string): ServerFrame | null {
  let frame: unknown;
  try {
    frame = JSON.parse(text);
  } catch {
    return null;
  }
  if (typeof frame !== "object" || frame === null) return null;
  const f = frame as Record<string, unknown>;
  const status = typeof f.status === "string" ? f.status : null;
  switch (f.type) {
    case "hello":
      if (!isCount(f.viewers) || !isCount(f.writers)) return null;
      return {
        type: "hello",
        status,
        target: f.target === "inspect" ? "inspect" : "session",
        viewers: f.viewers,
        writers: f.writers,
        seat: seatOf(f.seat),
        names: namesOf(f.names),
      };
    case "status":
      if (!isCount(f.viewers) || !isCount(f.writers)) return null;
      return {
        type: "status",
        status,
        viewers: f.viewers,
        writers: f.writers,
        seat: seatOf(f.seat),
        names: namesOf(f.names),
      };
    case "seat":
      return {
        type: "seat",
        ...seatOf(f),
        taken: f.taken === true,
        reason: typeof f.reason === "string" ? f.reason : null,
      };
    case "exit":
      return { type: "exit", reason: typeof f.reason === "string" ? f.reason : "" };
    default:
      return null;
  }
}

function side(value: number): number | null {
  return Number.isInteger(value) && value > 0 && value <= MAX_SIDE ? value : null;
}

/** The `resize` frame, or null for a size the gateway would refuse (1..65535 whole cells). */
export function resizeFrame(cols: number, rows: number): string | null {
  const c = side(cols);
  const r = side(rows);
  return c === null || r === null ? null : JSON.stringify({ type: "resize", cols: c, rows: r });
}

export function claimFrame(force: boolean): string {
  return JSON.stringify(force ? { type: "claim", force: true } : { type: "claim" });
}

/**
 * Where the gateway is, as a ws(s) origin: the one the API names, which under `make dev` and
 * `make up` is another port; else the API's origin, and on ECS (built with an empty API origin)
 * this origin, whose load balancer routes `/factorial/terminal/*` to the gateway.
 */
/** A browser keeps the session cookie per host, so a dashboard opened on 127.0.0.1 must dial the
 *  gateway there too, not on the `localhost` the API names. */
function sameLoopbackHost(gateway: string, origin: string): string {
  try {
    const g = new URL(gateway);
    const page = new URL(origin);
    const loopback = (h: string) => h === "localhost" || h === "127.0.0.1" || h === "[::1]";
    if (!loopback(g.hostname) || !loopback(page.hostname) || g.hostname === page.hostname)
      return "";
    g.hostname = page.hostname;
    return g.origin;
  } catch {
    return "";
  }
}

export function terminalBase(gateway: string, apiBase: string | undefined, origin: string): string {
  const base = sameLoopbackHost(gateway, origin) || gateway || apiBase || origin;
  return base.replace(/\/+$/, "").replace(/^http(s?):\/\//, "ws$1://");
}

export function terminalUrl(base: string, stepRunId: number, cols: number, rows: number): string {
  const address = `${base}${APP_PREFIX}/terminal/runs/${stepRunId}`;
  if (side(cols) === null || side(rows) === null) return address;
  return `${address}?${new URLSearchParams({ cols: String(cols), rows: String(rows) })}`;
}
