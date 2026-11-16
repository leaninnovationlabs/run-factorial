/**
 * The three ways a page tells its reader something, after ADS: `SectionMessage` in the flow of the
 * page, `InlineMessage` behind a small icon where the explanation would crowd the row, and `Banner`
 * across the top of the app for what is true of every page (offline, a new version deployed).
 *
 * Copy follows the ADS content standard: say what happened, then what to do next, in the "we"
 * voice — never a bare server string with no way forward.
 *
 * A success message that reports an action's outcome — a sync, a commit, a check — can be closed
 * once read (`onDismiss`, kept by `useDismissible`). Nothing else on a page closes: a warning or an
 * error is news the reader may still need, and an information message describes the page's state.
 */

import { type ReactNode, useId, useState } from "react";
import { Link } from "react-router-dom";
import { errorDetails, errorHint, errorText } from "../../format";
import { ROLE_ICON, type Role } from "../../status";
import { Button, IconButton, LinkButton } from "./Button";
import { ServerText } from "./Code";
import { cx } from "./cx";
import { Icon } from "./icons";
import { Popup } from "./Popup";

export type MessageAppearance = "information" | "warning" | "error" | "success" | "discovery";

/** A message's mark: a bar down its left edge in the role's colour, on the plain surface. The bar
 *  and the icon say which kind of message it is; a wash of colour made every message on a page
 *  louder than what it said, and drowned the buttons that act on it. */
const ROLE_EDGE: Record<Role, string> = {
  neutral: "border-l-bold",
  information: "border-l-information",
  warning: "border-l-warning",
  discovery: "border-l-discovery",
  danger: "border-l-danger",
  success: "border-l-success",
};

export function messageBox(role: Role): string {
  return cx(
    "rounded-l-sm rounded-r-md border border-l-3 border-default bg-surface",
    ROLE_EDGE[role],
  );
}

const MESSAGE_ROLE: Record<MessageAppearance, Role> = {
  information: "information",
  warning: "warning",
  error: "danger",
  success: "success",
  discovery: "discovery",
};

export type MessageAction = {
  text: string;
  onClick?: () => void;
  to?: string;
  href?: string;
  /** Drawn as buttons only: the action is under way, and pressing again does nothing. */
  isLoading?: boolean;
};

const ACTION = "font-medium underline-offset-2 hover:underline";

/** An action as a button, so it reads as one on the message's plain ground. */
export function ActionButton({ action }: { action: MessageAction }) {
  if (action.to !== undefined || action.href !== undefined) {
    return (
      <LinkButton to={action.to} href={action.href} spacing="compact">
        {action.text}
      </LinkButton>
    );
  }
  return (
    <Button spacing="compact" onClick={action.onClick} isLoading={action.isLoading}>
      {action.text}
    </Button>
  );
}

function Action({ action }: { action: MessageAction }) {
  if (action.to !== undefined) {
    return (
      <Link to={action.to} className={ACTION}>
        {action.text}
      </Link>
    );
  }
  if (action.href !== undefined) {
    return (
      <a href={action.href} target="_blank" rel="noreferrer" className={ACTION}>
        {action.text}
      </a>
    );
  }
  return (
    <button type="button" onClick={action.onClick} className={ACTION}>
      {action.text}
    </button>
  );
}

export function SectionMessage({
  appearance = "information",
  title,
  actions,
  actionsAs = "buttons",
  isAlert = false,
  onDismiss,
  className,
  children,
}: {
  appearance?: MessageAppearance;
  title?: ReactNode;
  actions?: MessageAction[];
  /** Buttons, so an action reads as one on the plain ground; `links` is for a message whose
   *  actions only lead somewhere to read more. */
  actionsAs?: "links" | "buttons";
  /** Announce it as it appears: only for a failure the reader's own action caused. A message that
   *  loads with the page is read in its place, not shouted. */
  isAlert?: boolean;
  /** Draws a close button: for a success message reporting an action's outcome, and only that. */
  onDismiss?: () => void;
  /** Layout only: the margin that places it. */
  className?: string;
  children?: ReactNode;
}) {
  const role = MESSAGE_ROLE[appearance];
  const icon = ROLE_ICON[role];
  return (
    <section
      role={isAlert ? "alert" : undefined}
      data-appearance={appearance}
      className={cx("flex gap-3 p-4 text-body", messageBox(role), className)}
    >
      <Icon glyph={icon.glyph} className={cx("mt-0.5", icon.ink)} />
      <div className="min-w-0 flex-1 space-y-1 text-default">
        {title && <h3 className="text-heading-xs">{title}</h3>}
        {children && <div className="break-words whitespace-pre-wrap">{children}</div>}
        {actions && actions.length > 0 && (
          <div
            className={cx(
              "flex flex-wrap pt-1",
              actionsAs === "buttons" ? "gap-2 pt-2" : "gap-x-4 gap-y-1",
            )}
          >
            {actions.map((action) =>
              actionsAs === "buttons" ? (
                <ActionButton key={action.text} action={action} />
              ) : (
                <Action key={action.text} action={action} />
              ),
            )}
          </div>
        )}
      </div>
      {onDismiss && (
        <IconButton
          icon="close"
          label="Dismiss"
          spacing="compact"
          className="-my-1 -mr-1 shrink-0"
          onClick={onDismiss}
        />
      )}
    </section>
  );
}

/**
 * An action's outcome until its message is closed: the result while it is shown, and the way to
 * close it. The next result is a new object, so it shows again; the request's own state, which
 * other controls read, is never touched.
 */
export function useDismissible<T>(result: T | null | undefined): [T | undefined, () => void] {
  const [dismissed, setDismissed] = useState<T | null>(null);
  const shown = result != null && result !== dismissed ? result : undefined;
  return [shown, () => setDismissed(result ?? null)];
}

/** The technical text behind a plain error, folded: an operator opens it, and nobody else has to
 *  read a stack of status lines to learn that something failed. */
export function ErrorDetails({ details }: { details: string }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <span className="block basis-full">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((shown) => !shown)}
        className={cx(ACTION, "text-body-sm")}
      >
        {open ? "Hide details" : "Show details"}
      </button>
      <code
        id={id}
        hidden={!open}
        className="mt-1 block font-mono text-body-sm break-words whitespace-pre-wrap text-subtle"
      >
        {details}
      </code>
    </span>
  );
}

/** A request's failure as a `SectionMessage`: the error's own message, announced, then the next
 *  step the API names and the technical text behind Details when it has them, with an optional
 *  way to try again. Nothing is drawn when there is no error. */
export function ErrorMessage({
  error,
  title,
  onRetry,
  className,
}: {
  error: unknown;
  title?: string;
  onRetry?: () => void;
  className?: string;
}) {
  if (!error) return null;
  const hint = errorHint(error);
  const details = errorDetails(error);
  return (
    <SectionMessage
      appearance="error"
      title={title}
      isAlert
      className={className}
      actions={onRetry ? [{ text: "Try again", onClick: onRetry }] : undefined}
    >
      <ServerText text={errorText(error)} />
      {hint && (
        <span className="block">
          <ServerText text={hint} />
        </span>
      )}
      {details && <ErrorDetails details={details} />}
    </SectionMessage>
  );
}

/**
 * A request that failed, beside the control that made it — for a control in a row or a header,
 * where a full `SectionMessage` would not fit. Announced: the reader who pressed is waiting.
 */
export function ActionError({ error }: { error: unknown }) {
  if (!error) return null;
  const hint = errorHint(error);
  const details = errorDetails(error);
  return (
    <span
      role="alert"
      className="inline-flex flex-wrap items-center gap-x-1 text-body-sm font-medium text-danger"
    >
      <Icon glyph="status-error" size="small" />
      {errorText(error)}
      {hint && (
        <>
          {" "}
          <span className="font-normal text-default">{hint}</span>
        </>
      )}
      {details && <ErrorDetails details={details} />}
    </span>
  );
}

/**
 * What a page says when its last refetch failed but it still has data on screen: keep the data,
 * say how old it is, and let the poll keep trying. Blanking a page on a network blip would move
 * everything the reader was looking at.
 */
export function StaleNote({ updatedAt }: { updatedAt: number | null }) {
  if (!updatedAt) return null;
  return (
    <p role="status" className="inline-flex items-center gap-1 text-body-sm text-warning">
      <Icon glyph="status-warning" size="small" />
      Couldn't refresh this page. Showing data from{" "}
      {new Date(updatedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}.
      Retrying.
    </p>
  );
}

const INLINE_ROLE = {
  info: "information",
  warning: "warning",
  error: "danger",
  confirmation: "success",
} as const satisfies Record<string, Role>;

/**
 * An explanation behind an icon, for a fact that is visible but needs a sentence to be understood:
 * why a cost is an estimate, why a readiness row is not checked. The icon is a real button, so a
 * keyboard and a touch screen reach the sentence as well as a mouse.
 */
export function InlineMessage({
  appearance = "info",
  title,
  secondaryText,
  iconLabel,
  children,
}: {
  appearance?: keyof typeof INLINE_ROLE;
  title?: string;
  /** Written beside the icon, so the trigger says something before it is opened. */
  secondaryText?: string;
  /** The button's name, read out. */
  iconLabel: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const icon = ROLE_ICON[INLINE_ROLE[appearance]];
  return (
    <Popup
      isOpen={open}
      onClose={() => setOpen(false)}
      label={title ?? iconLabel}
      placement="bottom-start"
      trigger={(props) => (
        <button
          {...props}
          type="button"
          aria-label={secondaryText ? undefined : iconLabel}
          onClick={() => setOpen((value) => !value)}
          className="inline-flex items-center gap-1 rounded-sm text-body-sm text-subtle hover:text-default"
        >
          <Icon glyph={icon.glyph} size="small" className={icon.ink} />
          {secondaryText}
        </button>
      )}
    >
      <div className="space-y-1 p-4">
        {title && <h3 className="text-heading-xs">{title}</h3>}
        <div className="text-body text-default">{children}</div>
      </div>
    </Popup>
  );
}

/** Across the top of the app, above every page, for a fact about all of them. */
export function Banner({
  appearance = "warning",
  action,
  children,
}: {
  appearance?: "warning" | "error" | "announcement";
  action?: { text: string; onClick: () => void };
  children: ReactNode;
}) {
  return (
    <div
      role={appearance === "error" ? "alert" : "status"}
      data-appearance={appearance}
      className={cx(
        "flex min-h-10 items-center justify-center gap-2 px-4 py-2 text-body font-medium",
        appearance === "error"
          ? "bg-danger-bold text-inverse"
          : appearance === "announcement"
            ? "bg-neutral-bold text-inverse"
            : "bg-warning-bold text-warning-inverse",
      )}
    >
      <Icon
        glyph={
          appearance === "error"
            ? "status-error"
            : appearance === "warning"
              ? "status-warning"
              : "status-information"
        }
      />
      <span>{children}</span>
      {action && (
        <button
          type="button"
          onClick={action.onClick}
          className="font-semibold underline underline-offset-2"
        >
          {action.text}
        </button>
      )}
    </div>
  );
}
