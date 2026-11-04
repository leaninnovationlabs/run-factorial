/**
 * One viewer's connection to a run's terminal, with the gateway's reconnect policy (decision 57).
 *
 * Framework-free so the policy is tested without a DOM.
 *
 * The writer seat: a person's keystroke while the seat is free sends `claim` and waits here, with
 * the keys after it, for the answer. The gateway drops keys sent before it says `yours`, so they go
 * only once it has; a refusal, or someone taking the seat meanwhile, drops them. Only a person
 * claims: the emulator answers tmux's queries on its own, and those answers wait for nothing. A
 * viewer that does not hold the seat sends no byte of input: the gateway would drop it anyway
 * (`attach -r`), and this is the second lock on the same door.
 */

import {
  CloseCode,
  claimFrame,
  type Hello,
  parseServerFrame,
  resizeFrame,
  type Seat,
  type SeatFrame,
} from "./protocol";

/** Why a connection stopped for good. */
export type Stopped =
  | "ended"
  | "parked"
  | "signed-out"
  | "forbidden"
  | "no-terminal"
  | "crowded"
  | "failed";

export type SeatState = Seat & {
  /** A claim is out and unanswered. */
  claiming: boolean;
  /** Who took the seat from this viewer, while they still hold it. */
  takenBy: string | null;
  /** Why the gateway refused this viewer's last claim, or freed its seat. */
  notice: string | null;
  /** Keys typed while the last claim was out went past `MAX_HELD` and were not sent. */
  dropped: boolean;
};

const NO_SEAT: SeatState = {
  holder: null,
  yours: false,
  claiming: false,
  takenBy: null,
  notice: null,
  dropped: false,
};

/** What a claim holds for its answer, in UTF-16 units: a burst of typing, not a large paste. */
const MAX_HELD = 4096;

export type SocketState =
  | { phase: "connecting" }
  /** Open, while the gateway attaches: until its hello. */
  | { phase: "attaching" }
  | ({ phase: "open"; seat: SeatState } & Omit<Hello, "type" | "seat">)
  | { phase: "retrying"; code: number; reason: string; delayMs: number }
  | { phase: "stopped"; kind: Stopped; code: number; reason: string };

export type Retry = "now" | "backoff" | "never";

/** What a close code means for this viewer: `CloseCode` in `orchestrator/terminal/protocol.py`. */
export function closePolicy(code: number): { retry: Retry; kind: Stopped } {
  switch (code) {
    case CloseCode.ENDED:
      return { retry: "never", kind: "ended" };
    case CloseCode.ABNORMAL:
    case CloseCode.RESTARTING:
    case CloseCode.UNREACHABLE:
      return { retry: "backoff", kind: "failed" };
    case CloseCode.TOO_SLOW:
      return { retry: "now", kind: "failed" };
    case CloseCode.SIGNED_OUT:
      return { retry: "never", kind: "signed-out" };
    case CloseCode.FORBIDDEN:
      return { retry: "never", kind: "forbidden" };
    case CloseCode.NO_TERMINAL:
      return { retry: "never", kind: "no-terminal" };
    case CloseCode.PARKED:
      return { retry: "never", kind: "parked" };
    case CloseCode.CROWDED:
      return { retry: "never", kind: "crowded" };
    default:
      // 1008, 1011 and anything this client does not know: a retry would meet the same answer.
      return { retry: "never", kind: "failed" };
  }
}

const BACKOFF_MAX_MS = 30_000;
/** A connection that stayed this long earns a fresh backoff; a shorter one keeps climbing, so a
 *  viewer that keeps falling behind does not reconnect, redraw and re-audit in a tight loop. */
const SETTLED_MS = 10_000;
/** Handshakes in a row that fail before any connection ever opened, while the session still
 *  verifies: a gateway that is down, switched off behind the proxy, or not admitting this origin. */
const MAX_FAILED_OPENS = 3;

/** 1, 2, 4 … 30 seconds. `attempt` counts the retries since the last connection that attached. */
export function backoffMs(attempt: number): number {
  return Math.min(BACKOFF_MAX_MS, 1000 * 2 ** attempt);
}

/** The sentence for a close that came with no reason of its own. */
function fallbackReason(code: number): string {
  switch (code) {
    case CloseCode.ENDED:
      return "The session ended.";
    case CloseCode.ABNORMAL:
      return "Can't connect to the terminal.";
    case CloseCode.RESTARTING:
      return "The terminal service is restarting.";
    case CloseCode.TOO_SLOW:
      return "This view fell behind the terminal's output.";
    default:
      return "The terminal closed unexpectedly. Reconnect to try again.";
  }
}

const SIGNED_OUT_REASON = "Your session has ended. Sign in again to use the terminal.";

export const UNREACHABLE_REASON =
  "The terminal isn't available. If this continues, ask an administrator to check the terminal service.";

export const BAD_ADDRESS_REASON = "The terminal address isn't valid for this page.";

/** Where one attempt connects. A ticket goes in `protocols`, never in `url`, which lands in
 *  access logs. */
export type Dial = { url: string; protocols?: string[] };

/** What `dial` throws when another attempt would meet the same answer. */
export class DialRefused extends Error {
  readonly kind: Stopped;

  constructor(kind: Stopped, reason: string) {
    super(reason);
    this.name = "DialRefused";
    this.kind = kind;
  }
}

export type SocketOptions = {
  /** Asked again for every attempt: a ticket opens one connection. */
  dial: (cols: number, rows: number) => Dial | Promise<Dial>;
  /** The view's size now, in cells: it is the size this connection attaches at. */
  size: () => { cols: number; rows: number };
  onOutput: (bytes: Uint8Array) => void;
  onState: (state: SocketState) => void;
  /** A new connection attached; tmux is about to redraw the whole screen. */
  onAttach?: () => void;
  /** A retry is scheduled: what the gateway refused may have moved on for good. */
  onRetrying?: () => void;
  /** After a connection failed before it opened: true while the session cookie still verifies,
   *  false once it does not, null when that could not be told. */
  probeSession: () => Promise<boolean | null>;
  createSocket?: (url: string, protocols?: string[]) => WebSocket;
};

const encoder = new TextEncoder();

export class TerminalSocket {
  private ws: WebSocket | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private attempt = 0;
  private failedOpens = 0;
  /** A connection has opened: the gateway is reachable and admits this origin, so a later
   *  handshake that fails is a restart, which keeps backing off however long it takes. */
  private everOpened = false;
  /** When the current connection's hello arrived. */
  private attachedAt: number | null = null;
  private hello: Omit<Hello, "type" | "seat"> | null = null;
  private seat: SeatState = NO_SEAT;
  /** Keys typed while a claim is out, sent once it is granted. */
  private held: string[] = [];
  private heldLength = 0;
  private exitReason: string | null = null;
  private done = false;
  /** Waiting on `dial`: a start meanwhile would fetch a second ticket. */
  private dialing = false;

  private readonly options: SocketOptions;

  constructor(options: SocketOptions) {
    this.options = options;
  }

  start(): void {
    if (this.done || this.ws || this.dialing) return;
    this.clearTimer();
    this.exitReason = null;
    this.attachedAt = null;
    const { cols, rows } = this.options.size();
    let dial: Dial | Promise<Dial>;
    try {
      dial = this.options.dial(cols, rows);
    } catch (error) {
      this.dialFailed(error);
      return;
    }
    if (!(dial instanceof Promise)) {
      this.open(dial);
      return;
    }
    this.dialing = true;
    this.options.onState({ phase: "connecting" });
    dial.then(
      (ready) => {
        this.dialing = false;
        if (!this.done) this.open(ready);
      },
      (error: unknown) => {
        this.dialing = false;
        if (!this.done) this.dialFailed(error);
      },
    );
  }

  private dialFailed(error: unknown): void {
    if (error instanceof DialRefused) {
      this.stop(error.kind, 0, error.message);
      return;
    }
    void this.failedBeforeOpen(CloseCode.ABNORMAL, "");
  }

  private open({ url, protocols }: Dial): void {
    const create =
      this.options.createSocket ??
      ((address: string, offered?: string[]) => new WebSocket(address, offered));
    let ws: WebSocket;
    try {
      ws = create(url, protocols);
    } catch {
      // A malformed URL or a ws:// gateway under an https page: retrying cannot change either.
      this.stop("failed", 0, BAD_ADDRESS_REASON);
      return;
    }
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    let opened = false;
    this.options.onState({ phase: "connecting" });

    ws.onopen = () => {
      opened = true;
      this.everOpened = true;
      this.failedOpens = 0;
      if (this.ws === ws) this.options.onState({ phase: "attaching" });
    };
    ws.onmessage = (event: MessageEvent) => {
      if (this.ws !== ws) return;
      if (typeof event.data === "string") this.onText(event.data);
      else if (event.data instanceof ArrayBuffer) this.options.onOutput(new Uint8Array(event.data));
    };
    ws.onclose = (event: CloseEvent) => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.hello = null;
      this.seat = NO_SEAT;
      this.dropHeld();
      if (this.done) return;
      if (!opened) {
        void this.failedBeforeOpen(event.code, event.reason);
        return;
      }
      this.onClose(event.code, event.reason);
    };
  }

  /** What the emulator sends: sent while this viewer holds the seat, and while it is free the
   *  first `typed` by a person claims it. False when it was dropped. */
  sendInput(data: string, typed: boolean): boolean {
    const ws = this.ws;
    if (!this.hello || ws?.readyState !== WebSocket.OPEN) return false;
    const { holder, yours, claiming, dropped } = this.seat;
    if (yours) {
      ws.send(encoder.encode(data));
      if (dropped && typed) {
        this.seat = { ...this.seat, dropped: false };
        this.emit();
      }
      return true;
    }
    if (!typed) return false;
    if (!claiming) {
      if (holder !== null || this.hello.target !== "session") return false;
      this.sendClaim(false);
    }
    return this.hold(data);
  }

  private hold(data: string): boolean {
    // Past the first drop nothing more is held: an Enter must not follow a paste that was lost.
    if (this.seat.dropped || this.heldLength + data.length > MAX_HELD) {
      if (!this.seat.dropped) {
        this.seat = { ...this.seat, dropped: true };
        this.emit();
      }
      return false;
    }
    this.held.push(data);
    this.heldLength += data.length;
    return true;
  }

  private dropHeld(): void {
    this.held = [];
    this.heldLength = 0;
  }

  /** Ask for the seat; `force` takes it from whoever holds it. */
  claim(force: boolean): void {
    if (!this.hello || this.seat.yours || this.seat.claiming) return;
    if (this.ws?.readyState !== WebSocket.OPEN) return;
    this.sendClaim(force);
  }

  private sendClaim(force: boolean): void {
    this.ws?.send(claimFrame(force));
    this.seat = { ...this.seat, claiming: true, takenBy: null, notice: null, dropped: false };
    this.emit();
  }

  resize(cols: number, rows: number): void {
    const frame = resizeFrame(cols, rows);
    if (frame && this.ws?.readyState === WebSocket.OPEN) this.ws.send(frame);
  }

  /** Reconnect at once, from a retry's wait or a stop the reader chose to try again. */
  retryNow(): void {
    if (this.done) return;
    this.attempt = 0;
    this.failedOpens = 0;
    this.start();
  }

  /** For good: the tab closed, or the reader signed out. */
  close(): void {
    this.done = true;
    this.clearTimer();
    const ws = this.ws;
    this.ws = null;
    if (ws && ws.readyState !== WebSocket.CLOSED && ws.readyState !== WebSocket.CLOSING) {
      ws.close(CloseCode.ENDED);
    }
  }

  private onText(text: string): void {
    const frame = parseServerFrame(text);
    if (!frame) return;
    switch (frame.type) {
      case "hello": {
        this.attachedAt = Date.now();
        const { type: _, seat, ...hello } = frame;
        this.hello = hello;
        this.seat = { ...NO_SEAT, ...seat };
        this.dropHeld();
        this.options.onAttach?.();
        this.emit();
        return;
      }
      case "status": {
        if (!this.hello) return;
        const { status, viewers, writers, seat, names } = frame;
        this.hello = { ...this.hello, status, viewers, writers, names };
        this.observe(seat);
        this.emit();
        return;
      }
      case "seat":
        if (!this.hello) return;
        this.answer(frame);
        this.emit();
        return;
      case "exit":
        this.exitReason = frame.reason;
    }
  }

  /** The seat as a `status` says it now. */
  private observe({ holder, yours }: Seat): void {
    const moved = holder !== this.seat.holder;
    this.seat = {
      ...this.seat,
      holder,
      yours,
      claiming: this.seat.claiming && !yours,
      takenBy: moved ? null : this.seat.takenBy,
      notice: moved ? null : this.seat.notice,
    };
    if (yours) this.sendHeld();
  }

  /** A claim's answer, or the news that this viewer lost the seat. A refusal is this claim's
   *  alone: the next keystroke claims again. */
  private answer({ holder, yours, taken, reason }: SeatFrame): void {
    this.seat = {
      holder,
      yours,
      claiming: false,
      takenBy: taken ? holder : null,
      notice: yours || taken ? null : reason,
      dropped: yours && this.seat.dropped,
    };
    if (yours) this.sendHeld();
    else this.dropHeld();
  }

  private sendHeld(): void {
    const keys = this.held.join("");
    this.dropHeld();
    if (keys && this.ws?.readyState === WebSocket.OPEN) this.ws.send(encoder.encode(keys));
  }

  private emit(): void {
    if (this.hello) this.options.onState({ phase: "open", ...this.hello, seat: this.seat });
  }

  private async failedBeforeOpen(code: number, reason: string): Promise<void> {
    // A refused handshake looks like any other dropped connection. If the reason is the cookie,
    // retrying would go on for ever, so ask the API before trying again.
    const signedIn = await this.options.probeSession().catch(() => null);
    if (this.done) return;
    if (signedIn === false) {
      this.stop("signed-out", CloseCode.SIGNED_OUT, SIGNED_OUT_REASON);
      return;
    }
    this.failedOpens += 1;
    if (!this.everOpened && this.failedOpens >= MAX_FAILED_OPENS) {
      this.stop("failed", CloseCode.ABNORMAL, UNREACHABLE_REASON);
      return;
    }
    this.onClose(code, reason);
  }

  private onClose(code: number, sent: string): void {
    const reason = sent || this.exitReason || fallbackReason(code);
    const { retry, kind } = closePolicy(code);
    if (retry === "never") {
      this.stop(kind, code, kind === "signed-out" && !sent ? SIGNED_OUT_REASON : reason);
      return;
    }
    if (this.attachedAt !== null && Date.now() - this.attachedAt >= SETTLED_MS) this.attempt = 0;
    this.attachedAt = null;
    const delayMs = retry === "now" && this.attempt === 0 ? 0 : backoffMs(this.attempt);
    this.attempt += 1;
    this.options.onState({ phase: "retrying", code, reason, delayMs });
    this.timer = setTimeout(() => {
      this.timer = null;
      this.start();
    }, delayMs);
    this.options.onRetrying?.();
  }

  private stop(kind: Stopped, code: number, reason: string): void {
    this.options.onState({ phase: "stopped", kind, code, reason });
  }

  private clearTimer(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }
}
