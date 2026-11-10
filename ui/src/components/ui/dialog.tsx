/**
 * Modal surfaces: the Drawer and the ModalDialog, and the confirmation built on the dialog.
 *
 * Both run on `useDialog`: a modal layer on the overlay stack (`layers.ts`), so the page behind is
 * inert and Escape reaches only the top layer; the focus on Close (or `initialFocusRef`) when it
 * opens and back on whatever had it when it closes; the page's scroll locked without the
 * scrollbar's width jumping. Each mounts only while open, so a server render never meets a portal.
 *
 * The slide out is why a surface owns an `open` flag of its own. Callers mount it on a piece of
 * state and drop it when that clears, so an unmanaged surface would vanish instantly however long
 * its exit transition claimed to be. `close` runs the transition first and calls `onClose` when it
 * has finished — timed, not on `transitionend`, which never arrives for a surface hidden mid-flight.
 */

import {
  type ReactNode,
  type Ref,
  type RefObject,
  useCallback,
  useEffect,
  useId,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { Button, IconButton } from "./Button";
import { cx } from "./cx";
import { Icon } from "./icons";
import { pushLayer } from "./layers";
import { ErrorMessage } from "./messages";
import { EXIT_MS, prefersReducedMotion } from "./motion";

function useDialog(onClose: () => void, initialFocusRef?: RefObject<HTMLElement | null>) {
  const titleId = useId();
  const layer = useRef<HTMLDivElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  // Read during the first render, before an `autoFocus` inside the content can take the focus in
  // the commit that mounts it.
  const [returnTo] = useState(() =>
    typeof document === "undefined" ? null : (document.activeElement as HTMLElement | null),
  );
  const [entered, setEntered] = useState(false);
  const leaving = useRef(false);
  // Callers pass an inline arrow, a new function on every render of a page that polls; held in a
  // ref so `close` is stable and the effect below runs once.
  const closer = useRef(onClose);
  useEffect(() => {
    closer.current = onClose;
  }, [onClose]);

  const close = useCallback(() => {
    if (leaving.current) return;
    leaving.current = true;
    setEntered(false);
    setTimeout(() => closer.current(), prefersReducedMotion() ? 0 : EXIT_MS);
  }, []);

  useEffect(() => {
    // One frame closed, then open, or the surface mounts already in place with nothing to
    // transition from.
    const frame = requestAnimationFrame(() => setEntered(true));
    const popLayer = layer.current ? pushLayer(layer.current, close) : () => {};
    if (!layer.current?.contains(document.activeElement)) {
      (initialFocusRef?.current ?? closeButton.current)?.focus();
    }
    const { body } = document;
    const previousOverflow = body.style.overflow;
    const previousPadding = body.style.paddingRight;
    const gutter = window.innerWidth - document.documentElement.clientWidth;
    body.style.overflow = "hidden";
    if (gutter > 0) body.style.paddingRight = `${gutter}px`;
    return () => {
      cancelAnimationFrame(frame);
      popLayer();
      body.style.overflow = previousOverflow;
      body.style.paddingRight = previousPadding;
      // Without scrolling: what opened the dialog may have moved while the page was covered,
      // and following it would jump the page the reader is coming back to.
      if (returnTo?.isConnected) returnTo.focus({ preventScroll: true });
    };
  }, [close, initialFocusRef, returnTo]);

  return { titleId, layer, closeButton, entered, close };
}

function Blanket({ entered, onClick }: { entered: boolean; onClick: () => void }) {
  return (
    // Hidden from assistive technology and out of the tab order: Close and Escape are the ways out
    // a keyboard or a screen reader is given; a press on the blanket is a pointer's shortcut.
    <div
      data-blanket
      aria-hidden="true"
      onClick={onClick}
      className={cx(
        "absolute inset-0 bg-blanket transition-opacity motion-reduce:transition-none",
        entered
          ? "opacity-100 duration-250 ease-out-bold"
          : "opacity-0 duration-200 ease-in-practical",
      )}
    />
  );
}

const DRAWER_WIDTH = {
  medium: "w-[54rem]",
  wide: "w-[78rem]",
  full: "w-full md:w-[95vw]",
} as const;

export type DrawerHandle = { close: () => void };

/**
 * A drawer over the right-hand edge of the page — the one surface that opens there, whether for
 * editing (the process configuration, the board's settings) or for reading (a transcript, an
 * issue's body, a preview). It slides in and dims the page behind it (decision 50).
 *
 * Fixed width, and it never pushes: the panel is `position: fixed`, so nothing under it moves.
 */
export function Drawer({
  title,
  subtitle,
  actions,
  width = "medium",
  bare = false,
  footer,
  initialFocusRef,
  handle,
  onClose,
  children,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  /** Beside the Close button: a copy, a link out. */
  actions?: ReactNode;
  /** `wide` has room for two panes — a file with an explorer beside it; `full`, for three. */
  width?: keyof typeof DRAWER_WIDTH;
  /** Hand the body's padding and scrolling to the child, for two panes that scroll apart. */
  bare?: boolean;
  footer?: ReactNode;
  initialFocusRef?: RefObject<HTMLElement | null>;
  /** Closes it from what it holds, sliding out as Close does. */
  handle?: Ref<DrawerHandle>;
  onClose: () => void;
  children: ReactNode;
}) {
  const { titleId, layer, closeButton, entered, close } = useDialog(onClose, initialFocusRef);
  useImperativeHandle(handle, () => ({ close }), [close]);
  if (typeof document === "undefined") return null;
  return createPortal(
    <div ref={layer} className="fixed inset-0 z-modal flex justify-end">
      <Blanket entered={entered} onClick={close} />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={cx(
          // A phone's drawer is the whole window: a rounded edge there would frame nothing.
          "relative flex h-full max-w-full flex-col overflow-hidden bg-surface-overlay shadow-overlay md:rounded-l-xl",
          DRAWER_WIDTH[width],
          "transition-transform motion-reduce:transition-none",
          entered
            ? "translate-x-0 duration-250 ease-out-bold"
            : "translate-x-full duration-200 ease-in-practical",
        )}
      >
        <header className="flex items-start justify-between gap-4 border-b border-default px-4 py-3 md:px-5">
          <div className="min-w-0">
            <h2 id={titleId} className="text-heading-sm text-default">
              {title}
            </h2>
            {subtitle && <div className="mt-0.5 text-body-sm text-subtle">{subtitle}</div>}
          </div>
          <div className="flex shrink-0 items-center gap-1">
            {actions}
            <IconButton ref={closeButton} icon="close" label="Close" onClick={close} />
          </div>
        </header>
        <div
          className={cx(
            "min-h-0 flex-1",
            bare ? "overflow-hidden" : "overflow-y-auto overscroll-contain px-4 py-4 md:px-5",
          )}
        >
          {children}
        </div>
        {footer && (
          <footer className="border-t border-default px-4 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] md:px-5">
            {footer}
          </footer>
        )}
      </div>
    </div>,
    document.body,
  );
}

/**
 * Long free text — a run summary, a failure reason, a review comment. `link` is the GitHub target
 * the row it came from shows, repeated here as a real anchor: once this is open it is the only
 * thing being read, so the way out to GitHub belongs where the reader already is.
 */
export function TextDrawer({
  title,
  subtitle,
  body,
  link,
  onClose,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  body: string;
  link?: { label: string; href: string | null };
  onClose: () => void;
}) {
  return (
    <Drawer title={title} subtitle={subtitle} onClose={onClose}>
      {link?.href != null && (
        <p className="mb-3">
          <a
            href={link.href}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-body underline underline-offset-2"
          >
            {link.label}
            <Icon glyph="link-external" size="small" />
          </a>
        </p>
      )}
      <p className="text-body leading-relaxed whitespace-pre-wrap text-default">{body}</p>
    </Drawer>
  );
}

const DIALOG_WIDTH = { small: "w-[25rem]", medium: "w-[37.5rem]", large: "w-[50rem]" } as const;

/**
 * A centred modal, for a decision that has to be made before anything else: in this app, only
 * the confirmation of something that cannot be undone (decision 50). The title is the page's
 * `h1` while it is open; the footer's primary action sits to the right of the secondary one.
 */
export function ModalDialog({
  title,
  appearance,
  width = "small",
  footer,
  initialFocusRef,
  onClose,
  children,
}: {
  title: string;
  appearance?: "warning" | "danger";
  width?: keyof typeof DIALOG_WIDTH;
  footer: ReactNode;
  initialFocusRef?: RefObject<HTMLElement | null>;
  onClose: () => void;
  children: ReactNode;
}) {
  const { titleId, layer, closeButton, entered, close } = useDialog(onClose, initialFocusRef);
  if (typeof document === "undefined") return null;
  return createPortal(
    <div
      ref={layer}
      className="fixed inset-0 z-modal flex items-start justify-center overflow-y-auto p-4 sm:pt-[12vh]"
    >
      <Blanket entered={entered} onClick={close} />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={cx(
          "relative flex max-w-full flex-col rounded-xl bg-surface-overlay shadow-overlay",
          DIALOG_WIDTH[width],
          "transition-[opacity,transform] motion-reduce:transition-none",
          entered
            ? "translate-y-0 opacity-100 duration-250 ease-out-bold"
            : "translate-y-2 opacity-0 duration-200 ease-in-practical",
        )}
      >
        <header className="flex items-start gap-3 px-6 pt-6 pb-2">
          {appearance && (
            <Icon
              glyph={appearance === "danger" ? "status-error" : "status-warning"}
              className={cx(
                "mt-1",
                appearance === "danger" ? "text-icon-danger" : "text-icon-warning",
              )}
            />
          )}
          <h1 id={titleId} className="min-w-0 flex-1 text-heading-md text-default">
            {title}
          </h1>
          <IconButton ref={closeButton} icon="close" label="Close" onClick={close} />
        </header>
        <div className="px-6 py-2 text-body text-default">{children}</div>
        <footer className="flex flex-wrap justify-end gap-2 px-6 pt-4 pb-6">{footer}</footer>
      </div>
    </div>,
    document.body,
  );
}

/**
 * The confirmation for an action that cannot be undone — delete a runtime, forget a credential,
 * remove a stored secret (decision 50). The title asks the question with its object ("Delete
 * runtime codex-team?"), the body says what is lost, the button says exactly what it does, and the
 * focus starts on Cancel, the safe answer. It stays open while the request runs and shows its
 * failure.
 */
export function ConfirmDialog({
  title,
  consequence,
  confirmLabel,
  appearance = "danger",
  isPending = false,
  error,
  onConfirm,
  onCancel,
}: {
  title: string;
  consequence: ReactNode;
  confirmLabel: string;
  appearance?: "warning" | "danger";
  isPending?: boolean;
  error?: unknown;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const cancel = useRef<HTMLButtonElement>(null);
  return (
    <ModalDialog
      title={title}
      appearance={appearance}
      initialFocusRef={cancel}
      onClose={onCancel}
      footer={
        <>
          <Button ref={cancel} onClick={onCancel}>
            Cancel
          </Button>
          <Button appearance={appearance} isLoading={isPending} onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div>{consequence}</div>
        <ErrorMessage error={error} />
      </div>
    </ModalDialog>
  );
}
