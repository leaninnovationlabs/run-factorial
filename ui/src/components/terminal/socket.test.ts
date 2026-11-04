/**
 * The terminal connection against a fake WebSocket: the reconnect policy per close code, the
 * session probe after a refused handshake, the writer seat — no input from a viewer that does not
 * hold it, and a first keystroke that claims it — and a ticket that travels as a subprotocol,
 * fresh for each attempt.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CloseCode, terminalBase, terminalUrl } from "./protocol";

/** A subprotocol a dial may offer, as the gateway's ticket handshake names one. */
const TICKET_PROTOCOL = "factorial.terminal";

import {
  BAD_ADDRESS_REASON,
  backoffMs,
  closePolicy,
  type Dial,
  DialRefused,
  type SocketOptions,
  type SocketState,
  TerminalSocket,
  UNREACHABLE_REASON,
} from "./socket";
import { FakeSocket } from "./testFakes";

let states: SocketState[] = [];

function connect(
  probe: () => Promise<boolean | null> = async () => true,
  dial: (cols: number, rows: number) => Dial | Promise<Dial> = (cols, rows) => ({
    url: terminalUrl("ws://gw", 7, cols, rows),
  }),
  extra: Partial<SocketOptions> = {},
) {
  const socket = new TerminalSocket({
    dial,
    size: () => ({ cols: 120, rows: 40 }),
    onOutput: () => {},
    onState: (state) => states.push(state),
    probeSession: probe,
    createSocket: (url, protocols) => new FakeSocket(url, protocols) as unknown as WebSocket,
    ...extra,
  });
  socket.start();
  return socket;
}

const last = () => FakeSocket.made.at(-1) as FakeSocket;
const state = () => states.at(-1);

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("WebSocket", FakeSocket);
  FakeSocket.made = [];
  states = [];
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("closePolicy", () => {
  it.each([
    [CloseCode.ENDED, "never"],
    [CloseCode.MALFORMED, "never"],
    [CloseCode.INTERNAL, "never"],
    [CloseCode.SIGNED_OUT, "never"],
    [CloseCode.FORBIDDEN, "never"],
    [CloseCode.NO_TERMINAL, "never"],
    [CloseCode.PARKED, "never"],
    [CloseCode.CROWDED, "never"],
    [CloseCode.ABNORMAL, "backoff"],
    [CloseCode.RESTARTING, "backoff"],
    [CloseCode.UNREACHABLE, "backoff"],
    [CloseCode.TOO_SLOW, "now"],
    [4999, "never"],
  ])("close %i retries %s", (code, retry) => {
    expect(closePolicy(code).retry).toBe(retry);
  });

  it("backs off 1, 2, 4, 8, 16, then 30 seconds for ever", () => {
    expect([0, 1, 2, 3, 4, 5, 6, 12].map(backoffMs)).toEqual([
      1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000,
    ]);
  });
});

describe("TerminalSocket", () => {
  it("asks for the view's size, and nothing else", () => {
    connect();
    expect(last().url).toBe("ws://gw/factorial/terminal/runs/7?cols=120&rows=40");
    expect(last().binaryType).toBe("arraybuffer");
  });

  it("claims the free seat on the first keystroke and sends the keys only once it is granted", () => {
    const socket = connect();
    last().open();
    expect(socket.sendInput("x", true)).toBe(false);
    last().hello();
    expect(socket.sendInput("é", true)).toBe(true);
    expect(socket.sendInput("\r", true)).toBe(true);
    expect(last().sent).toEqual(['{"type":"claim"}']);
    expect(state()).toMatchObject({ phase: "open", seat: { claiming: true, yours: false } });
    last().seat({ holder: "operator@example.com", yours: true });
    expect(last().binary).toEqual([new Uint8Array([0xc3, 0xa9, 0x0d])]);
    expect(socket.sendInput("y", true)).toBe(true);
    expect(last().binary.at(-1)).toEqual(new Uint8Array([0x79]));
    socket.resize(100, 30);
    expect(last().sent.at(-1)).toBe('{"type":"resize","cols":100,"rows":30}');
  });

  it("sends nothing while someone else holds the seat, even when asked to", () => {
    const socket = connect();
    last().open();
    last().hello({ seat: { holder: "teammate@example.com", yours: false } });
    expect(socket.sendInput("rm -rf /\r", true)).toBe(false);
    expect(last().sent).toEqual([]);
    socket.claim(true);
    expect(last().sent).toEqual(['{"type":"claim","force":true}']);
  });

  it("drops the keys held for a claim that someone else won", () => {
    const socket = connect();
    last().open();
    last().hello();
    socket.sendInput("ls", true);
    last().seat({
      holder: "teammate@example.com",
      reason: "teammate@example.com has the keyboard",
    });
    expect(state()).toMatchObject({
      seat: { holder: "teammate@example.com", claiming: false },
    });
    expect(socket.sendInput("\r", true)).toBe(false);
    expect(last().binary).toEqual([]);
  });

  it("says why a claim was refused, and claims again on the next keystroke", () => {
    const socket = connect();
    last().open();
    last().hello();
    socket.sendInput("ls", true);
    const why = "the gateway could not check this run";
    last().seat({ reason: why });
    expect(state()).toMatchObject({ seat: { holder: null, claiming: false, notice: why } });
    expect(socket.sendInput("ls", true)).toBe(true);
    expect(last().sent).toEqual(['{"type":"claim"}', '{"type":"claim"}']);
    expect(last().binary).toEqual([]);
  });

  it("never claims for what the terminal answers on its own, nor holds it with a person's keys", () => {
    const socket = connect();
    last().open();
    last().hello();
    expect(socket.sendInput("\x1b[?1;2c", false)).toBe(false);
    expect(last().sent).toEqual([]);
    socket.sendInput("y", true);
    socket.sendInput("\x1b[>0;276;0c", false);
    last().seat({ holder: "operator@example.com", yours: true });
    expect(last().binary).toEqual([new Uint8Array([0x79])]);
    // Once the keys reach the session, tmux is owed its replies.
    expect(socket.sendInput("\x1b[?1;2c", false)).toBe(true);
  });

  it("holds at most 4 KiB for a claim, and says the rest was not sent", () => {
    const socket = connect();
    last().open();
    last().hello();
    socket.sendInput("a".repeat(4000), true);
    expect(socket.sendInput("b".repeat(200), true)).toBe(false);
    expect(state()).toMatchObject({ seat: { claiming: true, dropped: true } });
    last().seat({ holder: "operator@example.com", yours: true });
    expect(last().binary.map((frame) => frame.length)).toEqual([4000]);
    expect(state()).toMatchObject({ seat: { dropped: true } });
    socket.sendInput("c", true);
    expect(state()).toMatchObject({ seat: { dropped: false } });
  });

  it("drops every key after the first it could not hold, so no Enter follows a dropped paste", () => {
    const socket = connect();
    last().open();
    last().hello();
    socket.sendInput("a".repeat(4000), true);
    expect(socket.sendInput("b".repeat(200), true)).toBe(false);
    expect(socket.sendInput("\r", true)).toBe(false);
    last().seat({ holder: "operator@example.com", yours: true });
    expect(last().binary.map((frame) => frame.length)).toEqual([4000]);
  });

  it("never sends keys held for one connection on the next", async () => {
    const socket = connect();
    last().open();
    last().hello();
    socket.sendInput("ls", true);
    last().drop(CloseCode.RESTARTING);
    await vi.advanceTimersByTimeAsync(1000);
    expect(FakeSocket.made).toHaveLength(2);
    last().open();
    last().hello();
    last().seat({ holder: "operator@example.com", yours: true });
    expect(last().binary).toEqual([]);
  });

  it("stops sending, and says who, when the seat is taken from it", () => {
    const socket = connect();
    last().open();
    last().hello();
    socket.sendInput("a", true);
    last().seat({ holder: "operator@example.com", yours: true });
    last().seat({ holder: "teammate@example.com", taken: true });
    expect(state()).toMatchObject({
      seat: { holder: "teammate@example.com", takenBy: "teammate@example.com" },
    });
    expect(socket.sendInput("b", true)).toBe(false);
    expect(last().binary).toHaveLength(1);
  });

  it("claims again after the seat freed for want of typing, and says why it freed", () => {
    const socket = connect();
    last().open();
    last().hello({ seat: { holder: "operator@example.com", yours: true } });
    const freed = "the keyboard was freed after 120 seconds without typing";
    last().seat({ reason: freed });
    expect(state()).toMatchObject({ seat: { holder: null, notice: freed } });
    expect(socket.sendInput("a", true)).toBe(true);
    expect(last().sent.at(-1)).toBe('{"type":"claim"}');
  });

  it("never claims an open session's seat", () => {
    const socket = connect();
    last().open();
    last().hello({ target: "inspect" });
    expect(socket.sendInput("a", true)).toBe(false);
    expect(last().sent).toEqual([]);
  });

  it("reconnects at once when it fell behind, but backs off when it keeps falling behind", () => {
    connect();
    const behind = (heldMs: number) => {
      last().open();
      last().hello();
      vi.advanceTimersByTime(heldMs);
      last().drop(CloseCode.TOO_SLOW, "behind");
      const retry = state();
      if (retry?.phase !== "retrying") throw new Error("expected a retry");
      vi.advanceTimersByTime(retry.delayMs);
      return retry.delayMs;
    };
    expect(behind(0)).toBe(0);
    expect(behind(500)).toBe(2000);
    expect(behind(500)).toBe(4000);
    // A connection that held for 10 s starts over: falling behind once more is news, not a loop.
    expect(behind(10_000)).toBe(0);
    expect(FakeSocket.made).toHaveLength(5);
  });

  it("backs off while the substrate is unreachable, and starts over once one attaches", () => {
    connect();
    const waits: number[] = [];
    for (let i = 0; i < 6; i++) {
      last().open();
      last().drop(CloseCode.UNREACHABLE, "sandbox not running");
      const retry = state();
      if (retry?.phase !== "retrying") throw new Error("expected a retry");
      waits.push(retry.delayMs);
      vi.advanceTimersByTime(retry.delayMs - 1);
      expect(FakeSocket.made).toHaveLength(i + 1);
      vi.advanceTimersByTime(1);
      expect(FakeSocket.made).toHaveLength(i + 2);
    }
    expect(waits).toEqual([1000, 2000, 4000, 8000, 16000, 30000]);
    last().open();
    last().hello();
    vi.advanceTimersByTime(10_000);
    last().drop(CloseCode.RESTARTING);
    expect(state()).toMatchObject({ phase: "retrying", delayMs: 1000 });
  });

  it("tells its owner of every retry it schedules, and of no stop", () => {
    const onRetrying = vi.fn();
    connect(undefined, undefined, { onRetrying });
    last().open();
    last().hello();
    last().drop(CloseCode.UNREACHABLE, "sandbox not running");
    expect(onRetrying).toHaveBeenCalledOnce();
    vi.advanceTimersByTime(1000);
    last().open();
    last().drop(CloseCode.RESTARTING);
    expect(onRetrying).toHaveBeenCalledTimes(2);
    vi.advanceTimersByTime(2000);
    last().open();
    last().hello();
    last().drop(CloseCode.ENDED);
    expect(state()).toMatchObject({ phase: "stopped", kind: "ended" });
    expect(onRetrying).toHaveBeenCalledTimes(2);
  });

  it("says sign in again, and stops, when a refused handshake was the session", async () => {
    const probe = vi.fn(async () => false);
    connect(probe);
    last().drop(CloseCode.ABNORMAL);
    await vi.runAllTimersAsync();
    expect(probe).toHaveBeenCalledOnce();
    expect(state()).toMatchObject({ phase: "stopped", kind: "signed-out" });
    expect(FakeSocket.made).toHaveLength(1);
  });

  it("retries with backoff when a refused handshake was not the session", async () => {
    connect(async () => true);
    last().drop(CloseCode.ABNORMAL);
    await vi.advanceTimersByTimeAsync(0);
    expect(state()).toMatchObject({
      phase: "retrying",
      delayMs: 1000,
      reason: "Can't connect to the terminal.",
    });
    await vi.advanceTimersByTimeAsync(1000);
    expect(FakeSocket.made).toHaveLength(2);
  });

  it("says a close it does not know, with no reason of its own, was unexpected", () => {
    connect();
    last().open();
    last().hello();
    last().drop(4999);
    expect(state()).toEqual({
      phase: "stopped",
      kind: "failed",
      code: 4999,
      reason: "The terminal closed unexpectedly. Reconnect to try again.",
    });
  });

  it("stops after three handshakes in a row fail while the session verifies", async () => {
    const socket = connect(async () => true);
    for (let i = 0; i < 3; i++) {
      last().drop(CloseCode.ABNORMAL);
      await vi.runAllTimersAsync();
    }
    expect(state()).toEqual({
      phase: "stopped",
      kind: "failed",
      code: 1006,
      reason: UNREACHABLE_REASON,
    });
    expect(FakeSocket.made).toHaveLength(3);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeSocket.made).toHaveLength(3);
    socket.retryNow();
    expect(FakeSocket.made).toHaveLength(4);
  });

  it("counts only handshakes in a row: one that opens starts the count over", async () => {
    connect(async () => true);
    const fail = async () => {
      last().drop(CloseCode.ABNORMAL);
      await vi.runAllTimersAsync();
    };
    await fail();
    await fail();
    last().open();
    last().drop(CloseCode.RESTARTING);
    await vi.runAllTimersAsync();
    await fail();
    await fail();
    expect(state()).toEqual({ phase: "connecting" });
    expect(FakeSocket.made).toHaveLength(6);
  });

  it("keeps backing off through a long gateway restart once a connection had opened", async () => {
    connect(async () => true);
    last().open();
    last().hello();
    last().drop(CloseCode.RESTARTING, "The gateway is restarting.");
    await vi.runOnlyPendingTimersAsync();
    for (let i = 0; i < 5; i++) {
      last().drop(CloseCode.ABNORMAL);
      await vi.advanceTimersByTimeAsync(0);
      expect(state()).toMatchObject({ phase: "retrying" });
      await vi.runOnlyPendingTimersAsync();
    }
    expect(FakeSocket.made).toHaveLength(7);
  });

  it("stops at once when the gateway's address cannot be opened", () => {
    const socket = new TerminalSocket({
      dial: () => ({ url: "not a url" }),
      size: () => ({ cols: 80, rows: 24 }),
      onOutput: () => {},
      onState: (next) => states.push(next),
      probeSession: async () => true,
      createSocket: () => {
        throw new SyntaxError("invalid URL");
      },
    });
    socket.start();
    expect(state()).toEqual({
      phase: "stopped",
      kind: "failed",
      code: 0,
      reason: BAD_ADDRESS_REASON,
    });
  });

  it("offers a fresh ticket for each attempt as a subprotocol, never in the URL", async () => {
    let minted = 0;
    connect(
      async () => null,
      async (cols, rows) => {
        minted += 1;
        return {
          url: terminalUrl("ws://gw", 7, cols, rows),
          protocols: [TICKET_PROTOCOL, `v1.ticket-${minted}.sig`],
        };
      },
    );
    await vi.advanceTimersByTimeAsync(0);
    expect(last().protocols).toEqual([TICKET_PROTOCOL, "v1.ticket-1.sig"]);
    expect(last().url).toBe("ws://gw/factorial/terminal/runs/7?cols=120&rows=40");
    last().open();
    last().hello();
    last().drop(CloseCode.RESTARTING);
    await vi.advanceTimersByTimeAsync(1000);
    expect(FakeSocket.made).toHaveLength(2);
    expect(last().protocols).toEqual([TICKET_PROTOCOL, "v1.ticket-2.sig"]);
    expect(FakeSocket.made.every((made) => !made.url.includes("ticket"))).toBe(true);
  });

  it("stops with the refusal's own sentence, and never opens a socket, when a ticket is refused", async () => {
    connect(
      async () => null,
      async () => {
        throw new DialRefused("forbidden", "The API token was refused.");
      },
    );
    await vi.advanceTimersByTimeAsync(0);
    expect(state()).toEqual({
      phase: "stopped",
      kind: "forbidden",
      code: 0,
      reason: "The API token was refused.",
    });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeSocket.made).toEqual([]);
  });

  it("opens nothing for a ticket that arrives after the connection closed", async () => {
    let resolve: (dial: Dial) => void = () => {};
    const ticket = new Promise<Dial>((done) => {
      resolve = done;
    });
    const socket = connect(
      async () => null,
      () => ticket,
    );
    socket.close();
    resolve({ url: "ws://gw/terminal/runs/7", protocols: [TICKET_PROTOCOL, "t"] });
    await vi.advanceTimersByTimeAsync(0);
    expect(FakeSocket.made).toEqual([]);
  });

  it("closes 1000 and never reconnects once closed", () => {
    const socket = connect();
    last().open();
    last().hello({ seat: { holder: "operator@example.com", yours: true } });
    const first = last();
    socket.close();
    expect(first.closedWith).toBe(1000);
    first.drop(CloseCode.RESTARTING);
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.made).toHaveLength(1);
    expect(socket.sendInput("x", true)).toBe(false);
  });
});

describe("terminalBase", () => {
  it("prefers the origin the API names and turns http into ws", () => {
    expect(terminalBase("http://localhost:8001/", "x", "o")).toBe("ws://localhost:8001");
  });

  it("falls back to the API's base, then to this origin when the API's is empty", () => {
    expect(terminalBase("", "https://api.example", "o")).toBe("wss://api.example");
    expect(terminalBase("", "", "https://factorial.example")).toBe("wss://factorial.example");
    expect(terminalBase("", undefined, "https://f.example")).toBe("wss://f.example");
  });
});
