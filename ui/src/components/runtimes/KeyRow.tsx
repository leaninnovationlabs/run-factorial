/**
 * One credential on the create page: its label, then one row holding the field and its
 * `Test key` button.
 *
 * The field is uncontrolled and named by its credential, so the page can read it off its one
 * form inside the Test and Create handlers; a value is never state, a prop or an attribute. The
 * button is not a submit button.
 */

import { Button, Field, TextArea, TextField, VisuallyHidden } from "../ui";

const REQUIRED = "Required to create the runtime.";

export interface KeyTest {
  onTest: () => void;
  isPending: boolean;
}

export function KeyRow({
  name,
  label,
  placeholder,
  multiline = false,
  isMissing,
  onEdit,
  test,
}: {
  /** The credential's name; the form field's name. */
  name: string;
  label: string;
  placeholder?: string;
  /** A pasted file, not a one-line key. */
  multiline?: boolean;
  /** A refused Create found it empty. */
  isMissing: boolean;
  onEdit: () => void;
  test: KeyTest;
}) {
  return (
    <Field label={label} isRequired errorMessage={isMissing ? REQUIRED : undefined}>
      {(control) => (
        <div className={multiline ? "flex items-start gap-2" : "flex items-center gap-2"}>
          {multiline ? (
            <TextArea
              {...control}
              isInvalid={control["aria-invalid"] === true}
              name={name}
              minimumRows={4}
              spellCheck={false}
              autoComplete="off"
              isMonospaced
              placeholder={placeholder}
              onChange={onEdit}
              className="min-w-0 flex-1"
            />
          ) : (
            <TextField
              {...control}
              isInvalid={control["aria-invalid"] === true}
              name={name}
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder={placeholder}
              onChange={onEdit}
              className="min-w-0 flex-1"
            />
          )}
          <Button type="button" isLoading={test.isPending} onClick={test.onTest}>
            Test key
            <VisuallyHidden> for {label}</VisuallyHidden>
          </Button>
        </div>
      )}
    </Field>
  );
}
