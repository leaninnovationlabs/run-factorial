/**
 * The terminal emulator itself. Only ever imported lazily (`TerminalTab.tsx`): xterm and its
 * stylesheet are a chunk of their own, loaded the first time a Terminal section expands, and never
 * part of the entry chunk (decision 57).
 *
 * Keys, by `typing`: `off` (someone else holds the writer seat, or nobody may type) turns xterm's
 * stdin off and leaves Escape and Tab to the page, so Escape closes the drawer and Tab moves on.
 * `free` (the seat is free, or this viewer's claim is out) takes keystrokes, whose first claims the
 * seat, but still leaves Escape and Tab to the page: neither is a reason to take the keyboard from
 * the agent. `seated` sends every key to the session, Escape included. Ctrl+] is always the way
 * out (WCAG 2.1.2).
 *
 * xterm also sends data nobody typed: its answers to tmux's queries (DA1, DA2, XTVERSION, colours)
 * on every attach. Each piece of data says whether it followed a person's key, paste or
 * composition, so that only a person claims the seat.
 */

import "@xterm/xterm/css/xterm.css";
import { FitAddon } from "@xterm/addon-fit";
import { Unicode11Addon } from "@xterm/addon-unicode11";
import { Terminal } from "@xterm/xterm";
import { type Ref, useEffect, useImperativeHandle, useRef } from "react";
import { documentTheme, monoFont, watchTheme } from "./xtermTheme";

export type Typing = "off" | "free" | "seated";

// Focus in and out (DECSET 1004), then SGR (1006) and X10 mouse reports.
// biome-ignore lint/suspicious/noControlCharactersInRegex: a report is ESC and what follows it
const REPORTS = /\x1b\[[IO]|\x1b\[<\d+;\d+;\d+[mM]|\x1b\[M[\s\S]{3}/g;

/** What xterm sends that a person did not type: a click or a focus change is not typing. */
export function dropReports(data: string): string {
  return data.replace(REPORTS, "");
}

// The alternate screens (47, 1047, 1049) and mouse tracking (9, 1000-1003, 1005, 1006, 1015,
// 1016). On the alternate screen xterm keeps no scrollback, and with mouse tracking on the wheel
// becomes a report that is dropped anyway; tmux redraws absolutely, so nothing else is lost.
const SWALLOWED = new Set([47, 1047, 1049, 9, 1000, 1001, 1002, 1003, 1005, 1006, 1015, 1016]);

/** Whether xterm ignores this DECSET or DECRST, so that each viewer scrolls back on its own. */
export function swallowedModes(params: readonly (number | number[])[]): boolean {
  // A sequence that mixes in any other mode goes to xterm whole: tmux sends these one at a time.
  return params.length > 0 && params.every((p) => typeof p === "number" && SWALLOWED.has(p));
}

/** Long enough for xterm's composition, which it sends from a timer of its own; far shorter than
 *  any wait for tmux to ask again. */
const GESTURE_MS = 100;

const MODIFIERS = new Set(["Shift", "Control", "Alt", "Meta", "AltGraph", "CapsLock"]);

/** Whether what xterm sends now follows a person's key, paste or composition. */
export class Gestures {
  private until = 0;

  mark(): void {
    this.until = Date.now() + GESTURE_MS;
  }

  /** A key a person pressed; a modifier alone types nothing. */
  key(key: string): void {
    if (!MODIFIERS.has(key)) this.mark();
  }

  get now(): boolean {
    return Date.now() < this.until;
  }

  /** Paste and composition, caught on the way down: xterm sends their data from its own
   *  listeners on the textarea. */
  listen(host: HTMLElement): () => void {
    const mark = () => this.mark();
    for (const type of ["paste", "compositionend"]) host.addEventListener(type, mark, true);
    return () => {
      for (const type of ["paste", "compositionend"]) host.removeEventListener(type, mark, true);
    };
  }
}

export function widenEmoji(term: Terminal): void {
  term.loadAddon(new Unicode11Addon());
  term.unicode.activeVersion = "11";
}

/**
 * Hand xterm `bytes` a slice at a time, each once xterm has parsed the one before, so a long
 * history does not hold the page for as long as it takes to parse all of it. `alive` false stops
 * it: the terminal closed part-way through.
 */
export function writeInSlices(
  write: (bytes: Uint8Array, done: () => void) => void,
  bytes: Uint8Array,
  done: () => void,
  alive: () => boolean,
  slice = 65_536,
): void {
  const from = (start: number) => {
    const end = Math.min(start + slice, bytes.length);
    write(bytes.subarray(start, end), () => {
      if (!alive()) return;
      if (end < bytes.length) from(end);
      else done();
    });
  };
  if (alive()) from(0);
}

/**
 * Clear the screen and the scrollback once xterm has parsed what was written before. CAN drops a
 * sequence that write cut short, where ESC would finish it, and a finished query is answered;
 * then RIS resets.
 */
export function clearInOrder(term: Terminal): void {
  term.write("\x18\x1bc");
}

export type XtermHandle = {
  /** With `done`, `bytes` go to xterm in slices, and `done` follows the last one's parse. */
  write: (bytes: Uint8Array, done?: () => void) => void;
  /** Clears the screen and the scrollback, after what was written before and before a new
   *  connection's redraw, and stops a sliced write still under way. */
  reset: () => void;
  focus: () => void;
  size: () => { cols: number; rows: number };
};

export type XtermViewProps = {
  typing: Typing;
  /** What xterm sends, with the reports dropped; `typed` when a person's key, paste or
   *  composition caused it. Never called while `typing` is off. */
  onData: (data: string, typed: boolean) => void;
  onResize: (cols: number, rows: number) => void;
  /** The view has its font and its size: a connection can attach at the right size now. */
  onReady: () => void;
  /** Ctrl+]. */
  onLeave: () => void;
  onFocusChange?: (focused: boolean) => void;
  ref?: Ref<XtermHandle>;
};

const FONT_SIZE = 13;
/** Longer than a frame, so a resize that runs over many frames fits once at its end. */
const REFIT_SETTLE_MS = 150;

/**
 * Run `fit` once a burst of calls has stopped for `REFIT_SETTLE_MS`. Each fit resizes the remote
 * session, and a pane easing open from a bar would otherwise tell it a dozen widths, the first
 * few only a handful of columns, and make whatever runs there redraw at each.
 */
export function whenSettled(fit: () => void): (() => void) & { cancel: () => void } {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const call = () => {
    clearTimeout(timer);
    timer = setTimeout(fit, REFIT_SETTLE_MS);
  };
  return Object.assign(call, { cancel: () => clearTimeout(timer) });
}

export default function XtermView({
  typing,
  onData,
  onResize,
  onReady,
  onLeave,
  onFocusChange,
  ref,
}: XtermViewProps) {
  const host = useRef<HTMLDivElement>(null);
  const terminal = useRef<Terminal | null>(null);
  const resets = useRef(0);
  // The handlers below are bound once, when xterm is built; these keep them current.
  const latest = useRef({ typing, onData, onResize, onReady, onLeave, onFocusChange });
  useEffect(() => {
    latest.current = { typing, onData, onResize, onReady, onLeave, onFocusChange };
    if (terminal.current) terminal.current.options.disableStdin = typing === "off";
  });

  useImperativeHandle(
    ref,
    () => ({
      write: (bytes, done) => {
        const term = terminal.current;
        if (!term) {
          done?.();
          return;
        }
        if (!done) {
          term.write(bytes);
          return;
        }
        const since = resets.current;
        writeInSlices(
          (slice, parsed) => term.write(slice, parsed),
          bytes,
          done,
          () => {
            // A new connection owns the screen, and its own hello starts loading again.
            if (resets.current !== since) return false;
            if (terminal.current === term) return true;
            // Nothing is left to draw into, so the caller must stop waiting.
            done();
            return false;
          },
        );
      },
      reset: () => {
        resets.current += 1;
        if (terminal.current) clearInOrder(terminal.current);
      },
      focus: () => terminal.current?.focus(),
      size: () => ({
        cols: terminal.current?.cols ?? 80,
        rows: terminal.current?.rows ?? 24,
      }),
    }),
    [],
  );

  useEffect(() => {
    const element = host.current;
    if (!element) return;
    const term = new Terminal({
      fontFamily: monoFont(),
      fontSize: FONT_SIZE,
      theme: documentTheme(),
      disableStdin: latest.current.typing === "off",
      cursorBlink: false,
      allowProposedApi: true,
      scrollback: 10000,
      // The chart colours were picked for marks, not text: this lifts any that read too faintly.
      minimumContrastRatio: 4.5,
    });
    const fitter = new FitAddon();
    term.loadAddon(fitter);
    widenEmoji(term);
    const gestures = new Gestures();
    const unlisten = gestures.listen(element);
    term.attachCustomKeyEventHandler((event) => {
      if (event.type === "keydown" && event.ctrlKey && event.code === "BracketRight") {
        event.preventDefault();
        latest.current.onLeave();
        return false;
      }
      const taken =
        latest.current.typing === "seated" || (event.key !== "Escape" && event.key !== "Tab");
      if (taken && event.type === "keydown") gestures.key(event.key);
      return taken;
    });
    const data = term.onData((text) => {
      const sent = dropReports(text);
      if (sent && latest.current.typing !== "off") latest.current.onData(sent, gestures.now);
    });
    const modes = (["h", "l"] as const).map((final) =>
      term.parser.registerCsiHandler({ prefix: "?", final }, swallowedModes),
    );
    const resized = term.onResize(({ cols, rows }) => latest.current.onResize(cols, rows));
    term.open(element);
    terminal.current = term;
    const focused = () => latest.current.onFocusChange?.(true);
    const blurred = () => latest.current.onFocusChange?.(false);
    term.textarea?.addEventListener("focus", focused);
    term.textarea?.addEventListener("blur", blurred);

    let disposed = false;
    let ready = false;
    // Ready only after the first settled fit: the socket dials at the size it reads then, and a
    // section still easing open from its bar would otherwise start the session a few columns wide.
    const refit = whenSettled(() => {
      if (disposed) return;
      // A hidden panel measures zero, which fit() would turn into a 1-cell terminal.
      if (element.clientWidth > 0 && element.clientHeight > 0) fitter.fit();
      if (!ready) {
        ready = true;
        latest.current.onReady();
      }
    });
    const observer = new ResizeObserver(refit);
    // Measured with the real font, or the cell size is the fallback's and every column is off.
    document.fonts
      .load(`${FONT_SIZE}px "JetBrains Mono"`)
      .catch(() => {})
      .finally(() => {
        if (disposed) return;
        observer.observe(element);
        // An element that measures zero reports no first observation.
        refit();
      });
    const unwatch = watchTheme(() => {
      term.options.theme = documentTheme();
    });

    return () => {
      disposed = true;
      refit.cancel();
      observer.disconnect();
      unwatch();
      unlisten();
      term.textarea?.removeEventListener("focus", focused);
      term.textarea?.removeEventListener("blur", blurred);
      data.dispose();
      for (const mode of modes) mode.dispose();
      resized.dispose();
      term.dispose();
      terminal.current = null;
    };
  }, []);

  return <div ref={host} className="h-full w-full" />;
}
