/**
 * Lozenge, StatusLozenge, Badge, Tag and TagGroup, after ADS.
 *
 * One lozenge for every state the dashboard names. `StatusLozenge` goes through `status.ts` — the
 * tone table, `TONE_ROLE` and the glossary — so a ticket's header, its attempt's tag and the row
 * that lists it cannot disagree about the colour or the word.
 */

import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import type { StepRunStatus } from "../../api/types";
import {
  CONDITION_LABELS,
  CONDITION_TONES,
  type ConditionTone,
  ROLE_BOLD,
  ROLE_ICON,
  ROLE_QUIET,
  ROLE_SUBTLE,
  type Role,
  RUN_LABELS,
  type RunShown,
  runTone,
  type TicketCondition,
  TONE_ROLE,
} from "../../status";
import { cx } from "./cx";
import { type Glyph, Icon } from "./icons";

export function Lozenge({
  appearance = "neutral",
  isBold = false,
  isQuiet = false,
  iconBefore,
  maxWidth = 200,
  children,
}: {
  appearance?: Role;
  isBold?: boolean;
  /** Neutral words on a milder wash (`ROLE_QUIET`); the icon keeps the role's ink. */
  isQuiet?: boolean;
  iconBefore?: Glyph;
  maxWidth?: number;
  children: ReactNode;
}) {
  return (
    <span
      data-appearance={appearance}
      style={{ maxWidth }}
      className={cx(
        "inline-flex h-5 shrink-0 items-center gap-1 rounded-sm px-1.5 text-body-sm font-semibold whitespace-nowrap",
        isBold ? ROLE_BOLD[appearance] : isQuiet ? ROLE_QUIET[appearance] : ROLE_SUBTLE[appearance],
      )}
    >
      {iconBefore && (
        <Icon
          glyph={iconBefore}
          size="small"
          className={isQuiet ? ROLE_ICON[appearance].ink : undefined}
        />
      )}
      <span className="truncate">{children}</span>
    </span>
  );
}

/** What a condition or a run status is called, and the tone it is drawn in. An unrecognised
 *  string — the wire is `str` on several of these — still renders, in the neutral role. */
export function statusFacts(status: TicketCondition | StepRunStatus | string): {
  label: string;
  tone: ConditionTone;
} {
  const condition = CONDITION_TONES[status as TicketCondition];
  if (condition) return { label: CONDITION_LABELS[status as TicketCondition], tone: condition };
  if (Object.hasOwn(RUN_LABELS, status)) {
    return {
      label: RUN_LABELS[status as RunShown],
      tone: runTone(status as RunShown),
    };
  }
  const words = status.replace(/_/g, " ");
  return { label: words.charAt(0).toUpperCase() + words.slice(1), tone: "neutral" };
}

export function StatusLozenge({ status }: { status: TicketCondition | StepRunStatus | string }) {
  const { label, tone } = statusFacts(status);
  return <Lozenge appearance={TONE_ROLE[tone]}>{label}</Lozenge>;
}

/** A count. `max` caps what is written (`99+`), never what is counted. */
export function Badge({
  appearance = "neutral",
  max = 99,
  children,
}: {
  appearance?: "neutral" | "inverse" | "danger" | "information";
  max?: number;
  children: number;
}) {
  return (
    <span
      data-appearance={appearance}
      className={cx(
        "inline-flex h-4 min-w-5 items-center justify-center rounded-xs px-1 text-body-sm tabular-nums",
        appearance === "inverse"
          ? "bg-neutral-bold text-inverse"
          : appearance === "danger"
            ? "bg-danger-bold text-inverse"
            : appearance === "information"
              ? "bg-information text-information"
              : "bg-neutral text-default",
      )}
    >
      {children > max ? `${max}+` : children}
    </span>
  );
}

const TAG =
  "inline-flex h-5 max-w-full shrink-0 items-center gap-1 rounded-sm border border-default bg-surface px-1.5 text-body-sm text-subtle";

/**
 * A label that files something: a process lane, a runtime type, what is applied to a view. A
 * `Tag` with `to` or `href` is a link; one with `onRemove` carries its own way out.
 */
export function Tag({
  children,
  to,
  href,
  elemBefore,
  onRemove,
  removeLabel,
}: {
  children: ReactNode;
  to?: string;
  href?: string;
  elemBefore?: ReactNode;
  onRemove?: () => void;
  /** The remove button's name; defaults to "Remove" and the tag's text. */
  removeLabel?: string;
}) {
  const body = (
    <>
      {elemBefore}
      <span className="truncate">{children}</span>
    </>
  );
  if (to !== undefined) {
    return (
      <Link to={to} className={cx(TAG, "hover:underline")}>
        {body}
      </Link>
    );
  }
  if (href !== undefined) {
    return (
      <a href={href} target="_blank" rel="noreferrer" className={cx(TAG, "hover:underline")}>
        {body}
      </a>
    );
  }
  return (
    <span className={cx(TAG, onRemove && "pr-0.5")}>
      {body}
      {onRemove && (
        <button
          type="button"
          onClick={onRemove}
          aria-label={
            removeLabel ?? `Remove ${typeof children === "string" ? children : ""}`.trim()
          }
          className="inline-flex size-4 items-center justify-center rounded-xs text-icon-subtle hover:bg-neutral-subtle-hovered hover:text-icon"
        >
          <Icon glyph="close" size="small" />
        </button>
      )}
    </span>
  );
}

export function TagGroup({ label, children }: { label?: string; children: ReactNode }) {
  return (
    <div
      {...(label ? { role: "group", "aria-label": label } : {})}
      className="flex flex-wrap items-center gap-1"
    >
      {children}
    </div>
  );
}
