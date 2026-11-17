/**
 * The create page's Sandbox step, under its cards, when the cloud sandbox is chosen: the sandbox
 * key with its own `Test key`, its outcome, and the template box a passing test opens.
 *
 * The key field is uncontrolled and named by its credential (see `KeyRow`). The template box is
 * always there, so the page does not jump when the test passes; until then it is disabled.
 */

import type { RuntimeTemplateBuild } from "../../api/types";
import { ErrorMessage, Select } from "../ui";
import type { CredentialCheckState } from "./CreateAuthFields";
import { KeyRow } from "./KeyRow";
import { buildLabel, Outcome } from "./RuntimeCredentials";
import { E2B_CREDENTIAL, E2B_KEY_FORM } from "./runtimes";

export function CreateSandboxFields({
  onEdit,
  missing,
  templates,
  template,
  onTemplate,
  test,
}: {
  onEdit: () => void;
  /** Credential names a refused Create found empty. */
  missing: ReadonlySet<string>;
  /** The builds the passed test listed, newest first; null until the key passes. */
  templates: RuntimeTemplateBuild[] | null;
  template: string | null;
  /** Null leaves the template to the runtime's page. */
  onTemplate: (pin: string | null) => void;
  /** The sandbox key's own check: its outcome is `result.e2b`. */
  test: CredentialCheckState;
}) {
  return (
    <div className="space-y-4">
      <KeyRow
        name={E2B_CREDENTIAL}
        label={E2B_KEY_FORM.label}
        placeholder={E2B_KEY_FORM.placeholder}
        isMissing={missing.has(E2B_CREDENTIAL)}
        onEdit={onEdit}
        test={{ onTest: test.onTest, isPending: test.isPending }}
      />
      <ErrorMessage error={test.error} />
      {test.result?.e2b && <Outcome title="Cloud sandbox" check={test.result.e2b} />}
      {templates && templates.length > 0 ? (
        <Select
          label="Template"
          value={template ?? ""}
          onChange={(event) => onTemplate(event.target.value || null)}
          options={[
            { value: "", label: "Choose later on the runtime page" },
            ...templates.map((build) => ({ value: build.pin, label: buildLabel(build, "") })),
          ]}
        />
      ) : (
        <Select
          label="Template"
          disabled
          value=""
          onChange={() => {}}
          options={[
            {
              value: "",
              label: templates
                ? "This team has no ready builds. Build one, then choose it on the runtime's page."
                : "Test the key to list the templates",
            },
          ]}
        />
      )}
    </div>
  );
}
