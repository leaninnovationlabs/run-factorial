/**
 * Stand-ins for the terminal's tests: a WebSocket the test drives by hand, and an xterm that
 * records what it was handed. happy-dom cannot run xterm (decision 51), so the emulator itself is
 * Chrome's to check.
 */

import { useEffect, useImperativeHandle } from "react";
import type { XtermViewProps } from "./XtermView";

export class FakeSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static made: FakeSocket[] = [];

  readyState = FakeSocket.CONNECTING;
  binaryType = "blob";
  sent: (string | Uint8Array)[] = [];
  closedWith: number | null = null;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: { code: number; reason: string }) => void) | null = null;

  readonly url: string;
  readonly protocols: string[] | undefined;

  constructor(url: string, protocols?: string[]) {
    this.url = url;
    this.protocols = protocols;
    FakeSocket.made.push(this);
  }

  send(data: string | Uint8Array) {
    this.sent.push(data);
  }

  close(code: number) {
    this.closedWith = code;
    this.readyState = FakeSocket.CLOSED;
  }

  open() {
    this.readyState = FakeSocket.OPEN;
    this.onopen?.();
  }

  hello(fields: Record<string, unknown> = {}) {
    this.text({
      type: "hello",
      status: "running",
      target: "session",
      viewers: 1,
      writers: 0,
      seat: { holder: null, yours: false },
      ...fields,
    });
  }

  seat(fields: Record<string, unknown> = {}) {
    this.text({ type: "seat", holder: null, yours: false, taken: false, reason: null, ...fields });
  }

  status(fields: Record<string, unknown> = {}) {
    this.text({
      type: "status",
      status: "running",
      viewers: 1,
      writers: 0,
      seat: { holder: null, yours: false },
      ...fields,
    });
  }

  text(frame: unknown) {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }

  drop(code: number, reason = "") {
    this.readyState = FakeSocket.CLOSED;
    this.onclose?.({ code, reason });
  }

  get binary(): Uint8Array[] {
    return this.sent.filter((frame): frame is Uint8Array => typeof frame !== "string");
  }
}

/** What the fake emulator saw. `loads` counts imports of the mocked module: one only once a
 *  Terminal section has rendered. `parsing` holds each write's callback, which xterm would call
 *  once it had parsed those bytes; a test calls them when it chooses. */
export const xterm: {
  loads: number;
  props: XtermViewProps | null;
  written: Uint8Array[];
  parsing: (() => void)[];
  resets: number;
  focuses: number;
} = { loads: 0, props: null, written: [], parsing: [], resets: 0, focuses: 0 };

export function FakeXterm(props: XtermViewProps) {
  xterm.props = props;
  useImperativeHandle(
    props.ref,
    () => ({
      write: (bytes: Uint8Array, done?: () => void) => {
        xterm.written.push(bytes);
        if (done) xterm.parsing.push(done);
      },
      reset: () => {
        xterm.resets += 1;
      },
      focus: () => {
        xterm.focuses += 1;
      },
      size: () => ({ cols: 100, rows: 30 }),
    }),
    [],
  );
  const { onReady } = props;
  useEffect(() => onReady(), [onReady]);
  return <div data-fake-xterm={props.typing} />;
}
