import { type ReactNode, useId, useState } from "react";
import { useCan } from "../../api/organizationQueries";
import { useCreateLabels } from "../../api/queries";
import type { ProjectDetail, ReadinessItem } from "../../api/types";
import { ROLE_ICON } from "../../status";
import {
  ActionButton,
  Button,
  CodeBlock,
  CopyButton,
  copyText,
  cx,
  ErrorMessage,
  Icon,
  type MessageAction,
  messageBox,
  SectionMessage,
  ServerText,
  showFlag,
  useDismissible,
} from "../ui";
import { ROW_TITLES } from "./readiness";

/** One thing wrong with a page's requirements: what, why, and the fix as buttons. */
export type Issue = {
  id: string;
  state: "blocked" | "warn";
  title: ReactNode;
  detail?: ReactNode;
  actions?: MessageAction[];
};

/** Every problem on a page in one box, a row each, on a faint wash of the worst one's colour.
 *  One box rather than one per problem: a page with three things wrong read as three alarms. */
export function IssueBox({ issues, className }: { issues: Issue[]; className?: string }) {
  if (issues.length === 0) return null;
  const role = issues.some((issue) => issue.state === "blocked") ? "danger" : "warning";
  return (
    <section
      data-appearance={role === "danger" ? "error" : "warning"}
      className={cx("text-body", messageBox(role), className)}
    >
      <ul className="divide-y divide-default">
        {issues.map((issue) => {
          const icon = ROLE_ICON[issue.state === "blocked" ? "danger" : "warning"];
          return (
            <li key={issue.id} data-issue={issue.id} className="flex gap-3 p-4">
              <Icon glyph={icon.glyph} className={cx("mt-0.5", icon.ink)} />
              <div className="min-w-0 flex-1 space-y-1">
                <h3 className="text-heading-xs text-default">{issue.title}</h3>
                {issue.detail && (
                  <div className="break-words whitespace-pre-wrap text-subtle">{issue.detail}</div>
                )}
                {issue.actions && issue.actions.length > 0 && (
                  <div className="flex flex-wrap gap-2 pt-2">
                    {issue.actions.map((action) => (
                      <ActionButton key={action.text} action={action} />
                    ))}
                  </div>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function issueOf(row: ReadinessItem, title: string, actions?: MessageAction[]): Issue | null {
  if (row.state !== "blocked" && row.state !== "warn") return null;
  return {
    id: `${row.key}-${row.process ?? ""}`,
    state: row.state,
    title,
    detail: row.detail ? <ServerText text={row.detail} /> : undefined,
    actions,
  };
}

/** What is wrong with a row that is not ready, and how to fix it. Nothing for a ready row. */
export function RowMessage({
  row,
  actions,
  title = row.summary,
}: {
  row: ReadinessItem;
  actions?: MessageAction[];
  title?: string;
}) {
  const issue = issueOf(row, title, actions);
  return issue ? <IssueBox issues={[issue]} /> : null;
}

/** The box that leads a configuration page: each of its rows that is not ready, named, with its
 *  fix. The runtime row is left to its body, which says what is wrong lane by lane. A fix that
 *  writes through the API is only for a reader who holds `projects:manage`; a link or a command to
 *  copy is anyone's. */
export function PageMessages({ project, rows }: { project: ProjectDetail; rows: ReadinessItem[] }) {
  const create = useCreateLabels(project.id);
  const canManage = useCan("projects:manage");
  const [created, dismissCreated] = useDismissible(create.data);
  const actionsFor = (row: ReadinessItem): MessageAction[] | undefined => {
    if (row.key === "app_access" && row.link) {
      return [{ text: "Open installation settings", href: row.link }];
    }
    if (row.key === "labels") {
      if (!canManage) return undefined;
      return [
        {
          text: "Create missing labels",
          isLoading: create.isPending,
          onClick: () => create.mutate(undefined),
        },
      ];
    }
    return row.remedy ? [copyCommand(row.remedy)] : undefined;
  };
  const issues = rows
    .filter((row) => row.key !== "runtime")
    // A summary is written for its own row ("not set"); above a page it needs its name.
    .flatMap(
      (row) => issueOf(row, `${ROW_TITLES[row.key]}: ${row.summary}`, actionsFor(row)) ?? [],
    );
  return (
    <>
      <IssueBox issues={issues} />
      {created && (
        <SectionMessage appearance="success" onDismiss={dismissCreated}>
          {created.created.length === 0
            ? "The repository already defines every label."
            : `Created ${created.created.length}: ${created.created.join(", ")}.`}
        </SectionMessage>
      )}
      <ErrorMessage error={create.error} title="Couldn't create the labels" />
    </>
  );
}

/** The opening paragraph every readiness body starts with. */
export const INTRO = "text-body text-subtle";

//: How wide a field is drawn, by what it holds: a field never stretches across a wide page, and
//: on a narrow one it still takes the width there is.
export const FIELD_WIDTH = {
  name: "max-w-sm",
  repository: "max-w-md",
  prose: "max-w-xl",
} as const;

/** A remedy's command as a message action: copied, with a flag saying whether it took. */
export function copyCommand(remedy: string): MessageAction {
  return {
    text: "Copy command",
    onClick: async () => {
      const copied = await copyText(remedy);
      showFlag(
        copied
          ? { appearance: "success", title: "Copied the command" }
          : {
              appearance: "error",
              title: "Couldn't copy the command",
              description: "Select it below and copy it by hand.",
            },
      );
    },
  };
}

/** A command to run, folded: a copy button, and the text itself only for a reader who opens it.
 *  These commands run to a dozen lines of JSON, which buried the page they sat on. */
export function RemedyCommand({
  remedy,
  label,
  name = label,
}: {
  remedy: string;
  label: string;
  /** What the copy button names, when several commands share one `label`. */
  name?: string;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-body-sm font-semibold text-subtle">{label}</span>
        <CopyButton value={remedy} name={name} />
        <Button
          spacing="compact"
          iconBefore={open ? "chevron-down" : "chevron-right"}
          aria-expanded={open}
          aria-controls={id}
          onClick={() => setOpen((previous) => !previous)}
        >
          {open ? "Hide command" : "Show command"}
        </Button>
      </div>
      <div id={id} hidden={!open}>
        <CodeBlock text={remedy} label={label} isCopyable={false} wrap />
      </div>
    </div>
  );
}
