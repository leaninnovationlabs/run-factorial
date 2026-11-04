/**
 * Flags, the announcer, and action feedback (ADS flag).
 *
 * The flags live in a small store outside React, so anything can raise one through `showFlag`: a
 * component, or the query client's error backstop, which runs outside every component.
 * `FlagGroup`, mounted once in the shell, draws them.
 *
 * A success or info flag dismisses itself after 8 s (paused while it is pointed at or focused); an
 * error or warning stays until dismissed, because it is news the reader may need to act on. Every
 * flag is also spoken through one polite live region, which is always mounted and rendered in
 * place — a region that appears together with its text is not reliably announced.
 */

import type { UseMutationResult } from "@tanstack/react-query";
import { type ReactNode, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { createPortal } from "react-dom";
import { ROLE_ICON, type Role } from "../../status";
import { cx } from "./cx";
import { Icon } from "./icons";

export type FlagAppearance = "success" | "info" | "warning" | "error";

export type FlagInput = {
  appearance: FlagAppearance;
  title: string;
  description?: ReactNode;
  actions?: { text: string; onClick: () => void }[];
};

type FlagItem = FlagInput & { id: number };

let flags: FlagItem[] = [];
let announcement = "";
let sequence = 0;
const listeners = new Set<() => void>();
let snapshot = { flags, announcement };

function emit() {
  snapshot = { flags, announcement };
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

const SERVER = { flags: [] as FlagItem[], announcement: "" };

/** Say something to a screen reader without drawing it. The same text twice is spoken twice. */
export function announce(text: string): void {
  announcement = "";
  emit();
  queueMicrotask(() => {
    announcement = text;
    emit();
  });
}

export function showFlag(input: FlagInput): number {
  sequence += 1;
  const id = sequence;
  flags = [...flags, { ...input, id }];
  emit();
  announce(
    typeof input.description === "string" ? `${input.title}. ${input.description}` : input.title,
  );
  return id;
}

function dismissFlag(id: number): void {
  flags = flags.filter((flag) => flag.id !== id);
  emit();
}

/** For tests: forget every flag. */
export function clearFlags(): void {
  flags = [];
  announcement = "";
  emit();
}

const FLAG_ROLE: Record<FlagAppearance, Role> = {
  success: "success",
  info: "information",
  warning: "warning",
  error: "danger",
};

export function FlagGroup() {
  const state = useSyncExternalStore(
    subscribe,
    () => snapshot,
    () => SERVER,
  );
  const announcer = (
    <div aria-live="polite" aria-atomic="true" className="sr-only">
      {state.announcement}
    </div>
  );
  return (
    <>
      {/* Beside `#root`, not in it: an open drawer or dialog makes `#root` inert, and an announcer
          inside it would leave the accessibility tree with the rest of the page. */}
      {typeof document !== "undefined" ? createPortal(announcer, document.body) : announcer}
      {state.flags.length > 0 &&
        typeof document !== "undefined" &&
        createPortal(
          <section
            aria-label="Notifications"
            className="fixed bottom-[calc(1rem+var(--tab-bar-clearance,0px))] left-4 z-flag flex w-[min(24rem,calc(100vw-2rem))] flex-col-reverse gap-2"
          >
            {state.flags.map((flag) => (
              <Flag key={flag.id} flag={flag} />
            ))}
          </section>,
          document.body,
        )}
    </>
  );
}

const AUTO_DISMISS_MS = 8_000;

function Flag({ flag }: { flag: FlagItem }) {
  const [held, setHeld] = useState(false);
  const icon = ROLE_ICON[FLAG_ROLE[flag.appearance]];
  const temporary = flag.appearance === "success" || flag.appearance === "info";
  useEffect(() => {
    if (!temporary || held) return;
    const timer = setTimeout(() => dismissFlag(flag.id), AUTO_DISMISS_MS);
    return () => clearTimeout(timer);
  }, [flag.id, held, temporary]);
  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: pausing the dismissal timer, not an action
    <div
      data-appearance={flag.appearance}
      onMouseEnter={() => setHeld(true)}
      onMouseLeave={() => setHeld(false)}
      onFocus={() => setHeld(true)}
      onBlur={() => setHeld(false)}
      className="flex gap-3 rounded-lg bg-surface-overlay p-4 text-body shadow-overlay"
    >
      <Icon glyph={icon.glyph} className={cx("mt-0.5", icon.ink)} />
      <div className="min-w-0 flex-1 space-y-1">
        <p className="text-heading-xs text-default">{flag.title}</p>
        {flag.description && <div className="text-subtle">{flag.description}</div>}
        {flag.actions && flag.actions.length > 0 && (
          <div className="flex flex-wrap gap-x-4 pt-1">
            {flag.actions.map((action) => (
              <button
                key={action.text}
                type="button"
                onClick={() => {
                  action.onClick();
                  dismissFlag(flag.id);
                }}
                className="font-medium hover:underline"
              >
                {action.text}
              </button>
            ))}
          </div>
        )}
      </div>
      <button
        type="button"
        aria-label="Dismiss"
        onClick={() => dismissFlag(flag.id)}
        className="-mt-1 -mr-1 inline-flex size-6 items-center justify-center rounded-md text-icon-subtle hover:bg-neutral-subtle-hovered hover:text-icon"
      >
        <Icon glyph="close" />
      </button>
    </div>
  );
}

/** How long a request runs before the page says it is still waiting — decision 49 lets refresh,
 *  exit, re-enter, interrupt and requeue wait up to 20 s for the worker's pass. */
export const SLOW_AFTER_MS = 3_000;
export const WAITING_FOR_WORKER = "Still waiting. This can take up to 20 seconds.";

/**
 * A request's whole answer, around the control that made it: `slow` turns true once it has run
 * for `slowAfterMs`, a failure raises an error flag with the API's own reason, and a success that
 * changes nothing visible can raise a success flag. The inline error beside the control stays the
 * page's to draw; this adds the flag that persists.
 */
export function useActionFeedback<TData, TVariables>(
  mutation: Pick<
    UseMutationResult<TData, Error, TVariables>,
    "status" | "error" | "submittedAt" | "isPending" | "data"
  >,
  {
    errorTitle,
    successTitle,
    slowAfterMs = SLOW_AFTER_MS,
  }: { errorTitle: string; successTitle?: (data: TData) => string | null; slowAfterMs?: number },
): { slow: boolean } {
  const [slow, setSlow] = useState(false);
  const reported = useRef(0);
  const { status, error, submittedAt, isPending, data } = mutation;

  useEffect(() => {
    if (!isPending) {
      setSlow(false);
      return;
    }
    const timer = setTimeout(() => setSlow(true), slowAfterMs);
    return () => clearTimeout(timer);
  }, [isPending, slowAfterMs]);

  useEffect(() => {
    if (reported.current === submittedAt) return;
    if (status === "error" && error) {
      reported.current = submittedAt;
      showFlag({ appearance: "error", title: errorTitle, description: error.message });
    } else if (status === "success") {
      reported.current = submittedAt;
      const title = successTitle && data !== undefined ? successTitle(data) : null;
      if (title) showFlag({ appearance: "success", title });
    }
  }, [status, error, submittedAt, data, errorTitle, successTitle]);

  return { slow };
}
