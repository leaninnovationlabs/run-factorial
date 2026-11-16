/**
 * Create an organization from inside one: its name and address, and nothing else. Its own page,
 * like `/members/invite`; Cancel returns to the overview. The page sits in the organization the
 * reader came from, so it says the new one is separate.
 *
 * Only a deployment that runs many organizations has it, as only that one has the API's route
 * (design.md §7.19).
 */

import { useAuthProviders } from "../api/organizationQueries";
import { OrganizationFields, useNewOrganization } from "../components/account/OrganizationFields";
import { NotFound } from "../components/shell/NotFound";
import {
  Button,
  ErrorMessage,
  Form,
  FormFooter,
  LinkButton,
  PageHeader,
  Panel,
  RequiredLegend,
  usePageTitle,
} from "../components/ui";

export default function NewOrganizationPage() {
  const providers = useAuthProviders();
  if (providers.isPending) return null;
  if (providers.data?.multi_org !== true) return <NotFound />;
  return <CreateOrganization />;
}

/** The tab's title is the form's own: NotFound names the tab too, and a title set above it would
 *  land after NotFound's, a parent's effect running after its child's. */
function CreateOrganization() {
  usePageTitle(["Create organization"]);
  const draft = useNewOrganization();
  return (
    <div className="space-y-6">
      <PageHeader title="Create organization" />
      <p className="text-body text-subtle">
        A separate organization, with its own projects, runtimes and members. You will be its owner.
      </p>
      <Panel>
        <Form
          aria-label="Create organization"
          validate={() => draft.valid}
          onSubmit={draft.submit}
          className="space-y-4 p-4"
        >
          {({ attempted }) => (
            <>
              <RequiredLegend />
              <OrganizationFields draft={draft} attempted={attempted} isRequired />
              <ErrorMessage error={draft.error} />
              <FormFooter align="end">
                <LinkButton to="/overview">Cancel</LinkButton>
                <Button type="submit" appearance="primary" isLoading={draft.busy}>
                  Create organization
                </Button>
              </FormFooter>
            </>
          )}
        </Form>
      </Panel>
    </div>
  );
}
