/**
 * The three ways a page tells its reader something, after ADS: `SectionMessage` in the flow of the
 * page, `InlineMessage` behind a small icon where the explanation would crowd the row, and `Banner`
 * across the top of the app for what is true of every page (offline, a new version deployed).
 *
 * Copy follows the ADS content standard: say what happened, then what to do next, in the "we"
 * voice — never a bare server string with no way forward.
 */

import { createContext, type ReactNode, useContext, useId, useState } from "react";
import { Link } from "react-router-dom";
import { errorDetails, errorHint, errorText } from "../../format";
import { ROLE_ICON, ROLE_WASH, type Role } from "../../status";
import { Button, LinkButton } from "./Button";
import { ServerText } from "./Code";
import { cx } from "./cx";
import { Icon } from "./icons";
import { Popup } from "./Popup";

export type MessageAppearance = "information" | "warning" | "error" | "success" | "discovery";

/** A quiet message's ground: the role's wash at a fraction of its strength, and no edge. */
const ROLE_QUIET_WASH: Record<Role, string> = {
  neutral: "bg-neutral",
  information: "bg-information/40",
  warning: "bg-warning/40",
  discovery: "bg-discovery/40",
  danger: "bg-danger/40",
  success: "bg-success/40",
};

const QuietContext = createContext(false);

/** Every `SectionMessage` inside draws on a fainter wash: for a page of settings, where a full
 *  block of colour per message made the page louder than what it said. */
export function QuietMessages({ children }: { children: ReactNode }) {
  return <QuietContext.Provider value>{children}</QuietContext.Provider>;
}

/** Whether messages here are quiet, for a composite that draws its own message box. */
export function useQuietMessages(): boolean {
  return useContext(QuietContext);
}

export function messageBox(role: Role, quiet: boolean): string {
  return quiet ? ROLE_QUIET_WASH[role] : ROLE_WASH[role];
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

/** An action as a button, for a message whose action is the fix rather than a way to read more. */
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
  actionsAs = "links",
  isAlert = false,
  className,
  children,
}: {
  appearance?: MessageAppearance;
  title?: ReactNode;
  actions?: MessageAction[];
  /** ADS draws a message's actions as links; `buttons` is for a message that leads a page and
   *  whose action is the fix itself. */
  actionsAs?: "links" | "buttons";
  /** Announce it as it appears: only for a failure the reader's own action caused. A message that
   *  loads with the page is read in its place, not shouted. */
  isAlert?: boolean;
  /** Layout only: the margin that places it. */
  className?: string;
  children?: ReactNode;
}) {
  const role = MESSAGE_ROLE[appearance];
  const icon = ROLE_ICON[role];
  const quiet = useContext(QuietContext);
  return (
    <section
      role={isAlert ? "alert" : undefined}
      data-appearance={appearance}
      className={cx("flex gap-3 rounded-md p-4 text-body", messageBox(role, quiet), className)}
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
    </section>
  );
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
