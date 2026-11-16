/**
 * The org-less pages multi-org adds (design.md §7.19): password sign-in, signup, onboarding,
 * joining by a pasted link, and an invite's link. The account comes first and the organization
 * second: a person with no organization lands on onboarding, and joins one only through an
 * invite's link.
 *
 * Leaving any of them for an organization is a full load: the organization is the router's
 * basename and fixed for a page's life (`org.tsx`).
 */

import { useQuery } from "@tanstack/react-query";
import { type ReactNode, useEffect, useState } from "react";
import { useLocation, useParams } from "react-router-dom";
import { ApiError, api } from "../api/client";
import {
  useAuthProviders,
  useMultiOrg,
  useOrganizations,
  useRoles,
} from "../api/organizationQueries";
import { useAuth } from "../auth";
import { OrganizationFields, useNewOrganization } from "../components/account/OrganizationFields";
import { LogoPlate } from "../components/LogoPlate";
import {
  Button,
  ErrorMessage,
  Form,
  SectionMessage,
  Spinner,
  TextField,
  usePageTitle,
} from "../components/ui";
import { inOrganization, orgless, rememberedOrganization } from "../org";
import { SsoDoors } from "./SsoDoors";

/** `access.accounts.PASSWORD_MIN`. */
export const PASSWORD_MIN = 12;

const INVITE_PATH = /^\/invite\/([A-Za-z0-9_-]+)$/;

/** Where signup returns: the invite link it was reached from, or onboarding. `next` is from a URL
 *  anyone can write, so only an invite's own path is taken. */
export function afterSignup(search: string): string {
  const next = new URLSearchParams(search).get("next");
  return next && INVITE_PATH.test(next) ? orgless(next) : orgless("/onboarding");
}

/** The invite's token, which signup sends: with one organization, an install that has data takes a
 *  password signup only from an invite's link. */
export function signupInvite(search: string): string | undefined {
  return INVITE_PATH.exec(new URLSearchParams(search).get("next") ?? "")?.[1];
}

/** The signup link from a page: it carries an invite's path, so the link is not lost. */
export function signupHref(pathname: string): string {
  return pathname.startsWith("/invite/")
    ? `${orgless("/signup")}?next=${encodeURIComponent(pathname)}`
    : orgless("/signup");
}

/** The token in a pasted invite link, or the pasted text when it is a bare token. */
export function inviteToken(pasted: string): string | null {
  const text = pasted.trim();
  const fromLink = /\/invite\/([^/?#\s]+)/.exec(text);
  if (fromLink) return fromLink[1] ?? null;
  return /^[A-Za-z0-9_-]{20,}$/.test(text) ? text : null;
}

/** The ground and card the sign-in page draws, for the account pages that stand outside an
 *  organization. */
export function AccountCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div
      data-app-shell
      className="sign-in-ground relative flex min-h-full items-center justify-center bg-surface-sunken px-6 py-16"
    >
      <div className="relative grid w-full max-w-2xl grid-cols-1 overflow-hidden rounded-lg border border-default bg-surface shadow-raised sm:grid-cols-2">
        <LogoPlate />
        <div className="flex flex-col justify-center gap-3 p-8">
          <h1 className="text-center text-heading-md text-default">{title}</h1>
          {children}
        </div>
      </div>
    </div>
  );
}

/** The password door on the sign-in card. Signs the browser in with the session cookie, then the
 *  app reads the session again and renders the page the URL names. The way to sign up shows where
 *  the deployment allows it, and on an invite's link, whose email may sign up in every shape. */
export function PasswordSignInForm({
  signup,
  forgot = false,
}: {
  signup: boolean;
  /** The deployment sends email, so a forgotten password can be reset. */
  forgot?: boolean;
}) {
  const { refreshSession } = useAuth();
  const { pathname } = useLocation();
  const offersSignup = signup || pathname.startsWith("/invite/");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit() {
    setError(null);
    setBusy(true);
    try {
      await api.passwordSignIn({ email, password });
      await refreshSession();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Form
      aria-label="Sign in with a password"
      validate={() => email.trim() !== "" && password !== ""}
      onSubmit={submit}
      className="space-y-3"
    >
      {({ attempted }) => (
        <>
          <TextField
            label="Email"
            type="email"
            autoComplete="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            errorMessage={attempted && email.trim() === "" ? "Enter your email." : undefined}
          />
          <TextField
            label="Password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            errorMessage={attempted && password === "" ? "Enter your password." : undefined}
          />
          <ErrorMessage error={error} />
          <Button type="submit" appearance="primary" shouldFitContainer isLoading={busy}>
            Sign in
          </Button>
          {forgot && (
            <p className="text-center text-body-sm text-subtle">
              <a href={orgless("/forgot-password")} className="font-medium underline">
                Forgot your password?
              </a>
            </p>
          )}
          {offersSignup && (
            <p className="text-center text-body-sm text-subtle">
              New here?{" "}
              <a href={signupHref(pathname)} className="font-medium underline">
                Create an account
              </a>
            </p>
          )}
        </>
      )}
    </Form>
  );
}

export function SignUpPage() {
  const { refreshSession } = useAuth();
  const { search } = useLocation();
  const next = afterSignup(search);
  const invite = signupInvite(search);
  const providers = useAuthProviders().data;
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [setupToken, setSetupToken] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  usePageTitle(["Create an account"]);
  const needsSetupToken = Boolean(providers?.setup_token) && invite === undefined;

  async function submit() {
    setError(null);
    setBusy(true);
    try {
      await api.signUp({
        name,
        email,
        password,
        invite,
        ...(needsSetupToken ? { setup_token: setupToken } : {}),
      });
      await refreshSession();
      window.location.assign(next);
    } catch (caught) {
      setError(caught);
      setBusy(false);
    }
  }

  return (
    <AccountCard title="Create your account">
      <Form
        aria-label="Create an account"
        validate={() =>
          name.trim() !== "" &&
          email.trim() !== "" &&
          password.length >= PASSWORD_MIN &&
          (!needsSetupToken || setupToken !== "")
        }
        onSubmit={submit}
        className="space-y-3"
      >
        {({ attempted }) => (
          <>
            <TextField
              label="Name"
              autoComplete="name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              errorMessage={attempted && name.trim() === "" ? "Enter your name." : undefined}
            />
            <TextField
              label="Email"
              type="email"
              autoComplete="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              errorMessage={attempted && email.trim() === "" ? "Enter your email." : undefined}
            />
            <TextField
              label="Password"
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              helperMessage={`At least ${PASSWORD_MIN} characters.`}
              errorMessage={
                attempted && password.length < PASSWORD_MIN
                  ? `Use at least ${PASSWORD_MIN} characters.`
                  : undefined
              }
            />
            {needsSetupToken && (
              <TextField
                label="Setup token"
                type="password"
                autoComplete="off"
                value={setupToken}
                onChange={(event) => setSetupToken(event.target.value)}
                helperMessage="The value of FACTORIAL_AUTH__BOOTSTRAP_TOKEN. The first account owns this install."
                errorMessage={attempted && setupToken === "" ? "Enter the setup token." : undefined}
              />
            )}
            <ErrorMessage error={error} />
            <Button type="submit" appearance="primary" shouldFitContainer isLoading={busy}>
              Create account
            </Button>
          </>
        )}
      </Form>
      <p className="text-center text-body-sm text-subtle">
        Already have an account?{" "}
        <a href={orgless("/")} className="font-medium underline">
          Sign in
        </a>
      </p>
      <SsoDoors
        intent="sign-up"
        next={next}
        google={providers?.google === true}
        oidc={providers?.oidc === true}
        oidcLabel={providers?.oidc_label || "SSO"}
      />
    </AccountCard>
  );
}

/** Who is signed in, and the way out: an org-less page is the only page such a person has. */
function SignedInAs() {
  const { sessionEmail, signOut } = useAuth();
  if (!sessionEmail) return null;
  return (
    <p className="text-center text-body-sm text-subtle">
      Signed in as {sessionEmail}.{" "}
      <span className="whitespace-nowrap">
        Not you?{" "}
        <a
          href={orgless("/")}
          className="font-medium underline"
          onClick={(event) => {
            event.preventDefault();
            signOut();
          }}
        >
          Sign out
        </a>
      </span>
    </p>
  );
}

/** Where an account with no organization lands. With many organizations it makes one, and an
 *  invite's link is pasted on a page of its own (`JoinPage`). With one, nobody makes another, so
 *  it waits for an owner's invite. A member of any organization goes into the one this browser
 *  last used while they are still in it, and otherwise into the first: leaving one lands here,
 *  and the organization this browser remembers is then the one just left. */
export function OnboardingPage() {
  const providersQuery = useAuthProviders();
  const organizationsQuery = useOrganizations();
  const multiOrg = providersQuery.data?.multi_org === true;
  const listed = organizationsQuery.data;
  const remembered = rememberedOrganization();
  const into = listed?.find((each) => each.slug === remembered) ?? listed?.[0] ?? null;
  const draft = useNewOrganization();
  usePageTitle([multiOrg ? "Create your organization" : "Get started"]);
  useEffect(() => {
    if (into) window.location.replace(inOrganization(into.slug, "/overview"));
  }, [into]);
  if (providersQuery.isPending || organizationsQuery.isPending || into) return null;
  if (!multiOrg) {
    // A list that failed says nothing about membership, so the card does not claim any.
    return organizationsQuery.isError ? (
      <AccountCard title="Get started">
        <ErrorMessage error={organizationsQuery.error} />
        <SignedInAs />
      </AccountCard>
    ) : (
      <AccountCard title="You aren't a member of this organization yet">
        <p className="text-center text-body text-subtle">
          Ask one of its owners for an invite, then open the link they send you.
        </p>
        <SignedInAs />
      </AccountCard>
    );
  }
  return (
    <AccountCard title="Create your organization">
      <Form
        aria-label="Create your organization"
        validate={() => draft.valid}
        onSubmit={draft.submit}
        className="space-y-3"
      >
        {({ attempted }) => (
          <>
            <OrganizationFields draft={draft} attempted={attempted} />
            <ErrorMessage error={draft.error} />
            <Button type="submit" appearance="primary" shouldFitContainer isLoading={draft.busy}>
              Create organization
            </Button>
          </>
        )}
      </Form>
      <p className="text-center text-body-sm text-subtle">
        Have an invite link?{" "}
        <a href={orgless("/join")} className="font-medium underline">
          Join an organization
        </a>
      </p>
      <SignedInAs />
    </AccountCard>
  );
}

/** Where an invite's link is pasted, by a person who has one and did not open it: onboarding
 *  links here for a person in no organization, the organization menu for a member. It opens the
 *  invite's own page, which says who sent it and accepts it. Making an organization instead is
 *  onboarding's for a person in none, and the page in the app for a member: onboarding would only
 *  move a member into the organization they have. */
export function JoinPage() {
  const multiOrg = useMultiOrg();
  const member = useOrganizations().data?.[0];
  const [pasted, setPasted] = useState("");
  const token = inviteToken(pasted);
  usePageTitle(["Join an organization"]);
  return (
    <AccountCard title="Join an organization">
      <Form
        aria-label="Join an organization"
        validate={() => token !== null}
        onSubmit={() => {
          if (token) window.location.assign(orgless(`/invite/${encodeURIComponent(token)}`));
        }}
        className="space-y-3"
      >
        {({ attempted }) => (
          <>
            <TextField
              label="Invite link"
              value={pasted}
              onChange={(event) => setPasted(event.target.value)}
              helperMessage="Ask someone in the organization to invite you, then paste their link."
              errorMessage={attempted && token === null ? "That isn't an invite link." : undefined}
            />
            <Button type="submit" appearance="primary" shouldFitContainer>
              Open invite
            </Button>
          </>
        )}
      </Form>
      {multiOrg && (
        <p className="text-center text-body-sm text-subtle">
          Or{" "}
          <a
            href={
              member ? inOrganization(member.slug, "/organizations/new") : orgless("/onboarding")
            }
            className="font-medium underline"
          >
            create your own organization
          </a>
        </p>
      )}
      <SignedInAs />
    </AccountCard>
  );
}

/** An invite's link. The preview needs a session, so a link alone tells a stranger nothing, and
 *  the API accepts it only for the account whose email it names. */
export function InvitePage() {
  const { token = "" } = useParams();
  const { sessionEmail, signOut } = useAuth();
  const preview = useQuery({
    queryKey: ["invite", token],
    queryFn: () => api.previewInvite(token),
    retry: false,
  });
  const roles = useRoles();
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  usePageTitle(["Invite"]);

  async function accept() {
    setError(null);
    setBusy(true);
    try {
      const joined = await api.acceptInvite(token);
      window.location.assign(inOrganization(joined.organization_slug, "/overview"));
    } catch (caught) {
      setError(caught);
      setBusy(false);
    }
  }

  if (preview.isPending) {
    return (
      <AccountCard title="Invite">
        <Spinner label="Loading the invite" />
      </AccountCard>
    );
  }
  if (preview.isError) {
    const refused = preview.error instanceof ApiError ? preview.error.message : null;
    return (
      <AccountCard title="This invite can't be used">
        <SectionMessage appearance="warning">
          {refused ?? "Couldn't load the invite."} Ask the person who invited you for a new link.
        </SectionMessage>
        <a href={orgless("/onboarding")} className="text-center font-medium underline">
          Go to the start page
        </a>
      </AccountCard>
    );
  }
  const invite = preview.data;
  const inviter = invite.created_by === "api-token" ? "An API token" : invite.created_by;
  const role = roles.data?.find((each) => each.key === invite.role)?.name ?? invite.role;
  if (!invite.for_you) {
    return (
      <AccountCard title="This invite is for another account">
        <p className="text-body text-subtle">
          You're signed in as {sessionEmail ?? "another account"}. The invite names a different
          email address. Sign out, then open the link again and sign in with that address.
        </p>
        <Button appearance="primary" shouldFitContainer onClick={signOut}>
          Sign out
        </Button>
      </AccountCard>
    );
  }
  return (
    <AccountCard title={`Join ${invite.organization_name}`}>
      <p className="text-body text-subtle">
        {inviter} invited you to join as {role}.
      </p>
      <ErrorMessage error={error} />
      <Button appearance="primary" shouldFitContainer isLoading={busy} onClick={accept}>
        Accept invite
      </Button>
    </AccountCard>
  );
}
