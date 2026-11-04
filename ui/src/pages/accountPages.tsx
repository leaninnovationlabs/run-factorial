/**
 * The org-less pages multi-org adds (design.md §7.19): password sign-in, signup, onboarding and
 * an invite's link. The account comes first and the organization second: a person with no
 * organization lands on onboarding, and joins one only through an invite's link.
 *
 * Leaving any of them for an organization is a full load: the organization is the router's
 * basename and fixed for a page's life (`org.tsx`).
 */

import { useQuery } from "@tanstack/react-query";
import { type ReactNode, useEffect, useState } from "react";
import { useLocation, useParams } from "react-router-dom";
import { ApiError, api, googleLoginUrl, oidcLoginUrl } from "../api/client";
import {
  useAuthProviders,
  useMultiOrg,
  useOrganizations,
  useRoles,
} from "../api/organizationQueries";
import { useAuth } from "../auth";
import { LogoPlate } from "../components/LogoPlate";
import {
  Button,
  buttonLook,
  cx,
  ErrorMessage,
  Form,
  Panel,
  SectionMessage,
  Spinner,
  TextField,
  usePageTitle,
} from "../components/ui";
import { inOrganization, orgless } from "../org";

/** `access.accounts.PASSWORD_MIN`. */
export const PASSWORD_MIN = 12;

/** A slug from a name, as the create form suggests it: what `POST /api/orgs` accepts, or "". */
export function slugFor(name: string): string {
  return (
    name
      .toLowerCase()
      .normalize("NFKD")
      // The accents NFKD splits off, so "é" becomes "e" rather than a dash.
      .replace(/[\u0300-\u036f]/g, "")
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 40)
      .replace(/-+$/, "")
  );
}

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
function AccountCard({ title, children }: { title: string; children: ReactNode }) {
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
export function PasswordSignInForm({ signup }: { signup: boolean }) {
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
      {providers?.google && (
        <a href={googleLoginUrl(next)} className={cx(buttonLook({ shouldFitContainer: true }))}>
          Continue with Google
        </a>
      )}
      {providers?.oidc && (
        <a href={oidcLoginUrl(next)} className={cx(buttonLook({ shouldFitContainer: true }))}>
          Continue with {providers.oidc_label}
        </a>
      )}
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
    </AccountCard>
  );
}

/** Where an account with no organization lands. With many organizations it makes one or joins
 *  one by invite. With one, nobody chooses: a member goes straight in, and anyone else waits for
 *  an owner's invite link, which opens on its own page. */
export function OnboardingPage() {
  const { sessionEmail, signOut } = useAuth();
  const providersQuery = useAuthProviders();
  const organizationsQuery = useOrganizations();
  const loaded = organizationsQuery.data;
  const organizations = loaded ?? [];
  const multiOrg = useMultiOrg();
  const single = providersQuery.data?.multi_org === false;
  const into = single ? (loaded?.[0] ?? null) : null;
  usePageTitle(["Get started"]);
  useEffect(() => {
    if (into) window.location.replace(inOrganization(into.slug, "/overview"));
  }, [into]);
  if (providersQuery.isPending || into || (single && organizationsQuery.isPending)) return null;
  return (
    <div className="min-h-full bg-surface-sunken px-4 py-10">
      <div className="mx-auto max-w-page-narrow space-y-6">
        <div>
          <h1 className="text-heading-lg text-default">Get started</h1>
          {sessionEmail && (
            <p className="mt-1 text-body text-subtle">Signed in as {sessionEmail}.</p>
          )}
        </div>
        {multiOrg && organizations.length > 0 && (
          <Panel title="Your organizations">
            <ul className="space-y-1 p-4">
              {organizations.map((each) => (
                <li key={each.slug}>
                  <a
                    href={inOrganization(each.slug, "/overview")}
                    className="font-medium underline"
                  >
                    {each.name}
                  </a>
                </li>
              ))}
            </ul>
          </Panel>
        )}
        {multiOrg ? (
          <CreateOrganization />
        ) : organizationsQuery.isError ? (
          <ErrorMessage error={organizationsQuery.error} />
        ) : (
          <SectionMessage
            appearance="information"
            title="You aren't a member of this organization yet"
            actions={[{ text: "Sign out", onClick: signOut }]}
          >
            Ask one of its owners for an invite, then open the link they send you.
          </SectionMessage>
        )}
        {multiOrg && <JoinOrganization />}
      </div>
    </div>
  );
}

function CreateOrganization() {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const shownSlug = slugEdited ? slug : slugFor(name);

  async function submit() {
    setError(null);
    setBusy(true);
    try {
      const created = await api.createOrganization({ name, slug: shownSlug });
      window.location.assign(inOrganization(created.slug, "/overview"));
    } catch (caught) {
      setError(caught);
      setBusy(false);
    }
  }

  return (
    <Panel title="Create an organization">
      <Form
        aria-label="Create an organization"
        validate={() => name.trim() !== "" && shownSlug.length >= 3}
        onSubmit={submit}
        className="space-y-3 p-4"
      >
        {({ attempted }) => (
          <>
            <TextField
              label="Name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              errorMessage={attempted && name.trim() === "" ? "Name the organization." : undefined}
            />
            <TextField
              label="Address"
              isMonospaced
              value={shownSlug}
              onChange={(event) => {
                setSlugEdited(true);
                setSlug(event.target.value.toLowerCase());
              }}
              helperMessage="Used in every link to the organization. You can't change it later."
              errorMessage={
                attempted && shownSlug.length < 3 ? "Use at least 3 characters." : undefined
              }
            />
            <ErrorMessage error={error} />
            <Button type="submit" appearance="primary" isLoading={busy}>
              Create organization
            </Button>
          </>
        )}
      </Form>
    </Panel>
  );
}

function JoinOrganization() {
  const [pasted, setPasted] = useState("");
  const token = inviteToken(pasted);
  return (
    <Panel title="Join an organization">
      <Form
        aria-label="Join an organization"
        validate={() => token !== null}
        onSubmit={() => {
          if (token) window.location.assign(orgless(`/invite/${encodeURIComponent(token)}`));
        }}
        className="space-y-3 p-4"
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
            <Button type="submit">Open invite</Button>
          </>
        )}
      </Form>
    </Panel>
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
