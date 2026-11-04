/** The create page's aside: what the choices add up to, what will be stored, and the buttons. */

import { Button, Code, FormFooter, Icon, LinkButton, Panel } from "../ui";
import { type Choice, choiceLabels, typeFor } from "./runtimeChoice";

export function CreateSummary({
  choice,
  secrets,
  hasTemplate,
  problem,
  isPending,
}: {
  choice: Choice;
  /** The credential names Create sends. */
  secrets: string[];
  hasTemplate: boolean;
  /** Why the choice cannot be created; Create is disabled while there is one. */
  problem: string | null;
  isPending: boolean;
}) {
  const labels = choiceLabels(choice);
  const cloud = choice.sandbox === "cloud";
  const rows: [string, string][] = [
    ["CLI", labels.cli],
    ["Authentication", labels.auth],
    ["Sandbox", labels.sandbox],
  ];
  return (
    <Panel title="Summary" className="lg:sticky lg:top-4">
      <div className="space-y-4 p-4">
        <dl className="space-y-3 text-body">
          {rows.map(([term, value]) => (
            <div key={term}>
              <dt className="text-body-sm text-subtle">{term}</dt>
              <dd className="font-medium text-default">{value}</dd>
            </div>
          ))}
          <div>
            <dt className="text-body-sm text-subtle">Stores</dt>
            <dd className="flex flex-wrap gap-1">
              {secrets.length > 0 ? (
                secrets.map((name) => <Code key={name}>{name}</Code>)
              ) : (
                <span className="text-subtle">
                  {typeFor(choice.cli, choice.auth) === "codex_subscription"
                    ? "Nothing yet. You sign in with ChatGPT on the runtime's page after you create it."
                    : "Nothing. Each run gets a short-lived token from the host's AWS role."}
                </span>
              )}
            </dd>
          </div>
        </dl>
        {cloud && (
          <ul className="space-y-2 text-body-sm text-subtle">
            <Note>Each project must permit this runtime before its steps can use it.</Note>
            {!hasTemplate && <Note>Needs a template before it can run.</Note>}
          </ul>
        )}
        {problem && (
          <p role="status" className="text-body-sm text-danger">
            {problem}
          </p>
        )}
        <FormFooter align="end">
          <LinkButton to="/runtimes" appearance="subtle">
            Cancel
          </LinkButton>
          <Button
            type="submit"
            appearance="primary"
            isDisabled={problem !== null || isPending}
            isLoading={isPending}
          >
            Create runtime
          </Button>
        </FormFooter>
      </div>
    </Panel>
  );
}

function Note({ children }: { children: string }) {
  return (
    <li className="flex items-start gap-2">
      <Icon glyph="status-information" size="small" className="mt-0.5 text-icon-information" />
      <span>{children}</span>
    </li>
  );
}
