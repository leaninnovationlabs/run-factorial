import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { ApiError, api, googleLoginUrl, oidcLoginUrl } from "../api/client";
import { useAuth } from "../auth";
import { LogoPlate } from "../components/LogoPlate";
import { buttonLook, cx, SectionMessage, usePageTitle } from "../components/ui";
import { inOrganization, isOrgless, orgless, useOrg } from "../org";
import { APP_PREFIX } from "../prefix";
import { PasswordSignInForm } from "./accountPages";

/** How long the page waits before asking `/auth/providers` again when the API did not answer. */
export const PROVIDERS_RETRY_MS = 5000;

/** Why the Google round trip came back without a session (`api/routes/auth.py`'s callback). */
export type SignInNotice = "refused" | "retry";

/** The `?signin=` word the callback sends back, or null for anything it does not send. */
export function signInNotice(search: string): SignInNotice | null {
  const word = new URLSearchParams(search).get("signin");
  return word === "refused" || word === "retry" ? word : null;
}

/** Where the round trip returns: this route and its query, less the `signin` word, so a second
 *  try does not come back still saying the first one failed. */
export function withoutNotice({ pathname, search }: { pathname: string; search: string }): string {
  const params = new URLSearchParams(search);
  params.delete("signin");
  const rest = params.toString();
  return rest ? `${pathname}?${rest}` : pathname;
}

/** The step page a `/sign-in?next=` returns to, or the dashboard for anything else: `next` comes
 *  from a URL anyone can write, and this screen exists only for a page link. */
export function pageLink(search: string): string {
  const next = new URLSearchParams(search).get("next");
  return next?.startsWith(`${APP_PREFIX}/pages/`) ? next : "/";
}

/**
 * Every door signs the browser in with the same session cookie: a password, which every
 * deployment has, and Google or another OIDC issuer where `/auth/providers` says one is
 * configured. Nobody signs in with the API token (design.md §7.19).
 *
 * `forPage` is the screen a step's page link sends a browser to when it has no session
 * (`api/routes/pages.py`): the same doors, under a heading that says why. `forVerify` is the one
 * a verification link meets: only a session of the account it was sent to can redeem it.
 */
export function SignInPage({
  forPage = false,
  forVerify = false,
}: {
  forPage?: boolean;
  forVerify?: boolean;
}) {
  const { expired, sessionEmail } = useAuth();
  // Rendered in place of whatever route was asked for, so the URL is still that route: a
  // password sign-in comes back to it by itself, and an SSO door is told where to return.
  const location = useLocation();
  const org = useOrg();
  const [google, setGoogle] = useState(false);
  const [oidc, setOidc] = useState(false);
  const [oidcLabel, setOidcLabel] = useState("SSO");
  const [signup, setSignup] = useState(false);
  const [mail, setMail] = useState(false);
  usePageTitle([expired ? "Session expired" : "Sign in"]);

  // A password sign-in only reads the session again; an SSO one comes back to the link by itself.
  useEffect(() => {
    if (forPage && sessionEmail) window.location.assign(pageLink(location.search));
  }, [forPage, sessionEmail, location.search]);

  useEffect(() => {
    let cancelled = false;
    let retry: ReturnType<typeof setTimeout> | undefined;
    const ask = () => {
      api
        .authProviders()
        .then((providers) => {
          if (cancelled) return;
          if (providers.google) setGoogle(true);
          if (providers.signup) setSignup(true);
          if (providers.mail) setMail(true);
          if (providers.oidc) {
            setOidc(true);
            if (providers.oidc_label) setOidcLabel(providers.oidc_label);
          }
        })
        .catch((caught: unknown) => {
          // A refusal is an API without /auth/providers, and the password is the only door. No
          // answer, or the ALB's 503 while a deploy replaces the task, is asked again: a page
          // loaded in that gap would otherwise hide the SSO doors until it is reloaded.
          const refused = caught instanceof ApiError && caught.status > 0 && caught.status < 500;
          if (!cancelled && !refused) retry = setTimeout(ask, PROVIDERS_RETRY_MS);
        });
    };
    ask();
    return () => {
      cancelled = true;
      clearTimeout(retry);
    };
  }, []);

  return (
    <div
      data-app-shell
      className="sign-in-ground relative flex min-h-full items-center justify-center bg-surface-sunken px-6 py-16"
    >
      <SignInGround />
      <SignInCard
        google={google}
        oidc={oidc}
        oidcLabel={oidcLabel}
        signup={signup}
        forgot={mail}
        expired={expired}
        notice={signInNotice(location.search)}
        next={
          forPage
            ? pageLink(location.search)
            : isOrgless(location.pathname)
              ? orgless(withoutNotice(location))
              : inOrganization(org, withoutNotice(location))
        }
        forPage={forPage}
        forVerify={forVerify}
      />
    </div>
  );
}

type SignInCardProps = {
  google: boolean;
  oidc?: boolean;
  oidcLabel?: string;
  /** The deployment lets a stranger create an account here (`/auth/providers`). */
  signup?: boolean;
  /** The deployment sends email, so a forgotten password can be reset. */
  forgot?: boolean;
  /** Reached through a 401 rather than by arriving signed out. */
  expired: boolean;
  /** Back from an SSO sign-in that did not end in a session. */
  notice?: SignInNotice | null;
  /** The path to come back to after the SSO round trip. */
  next: string;
  /** Signing in to read a step's page. */
  forPage?: boolean;
  /** Signing in to redeem a verification link. */
  forVerify?: boolean;
};

/** The password form first, then each SSO door as one button of the same height and hairline. */
export function SignInCard({
  google,
  oidc = false,
  oidcLabel = "SSO",
  signup = false,
  forgot = false,
  expired,
  notice = null,
  next,
  forPage = false,
  forVerify = false,
}: SignInCardProps) {
  return (
    <div className="relative grid w-full max-w-2xl grid-cols-1 overflow-hidden rounded-lg border border-default bg-surface shadow-raised sm:grid-cols-2">
      <LogoPlate />
      <div className="flex flex-col justify-center gap-3 p-8">
        {forPage ? (
          <div className="text-center">
            <h1 className="text-heading-md text-default">Sign in to read this page</h1>
            <p className="mt-1 text-body text-subtle">The page opens after you sign in.</p>
          </div>
        ) : forVerify ? (
          <div className="text-center">
            <h1 className="text-heading-md text-default">Sign in to verify your email</h1>
            <p className="mt-1 text-body text-subtle">
              Use the account you created. The link works only there.
            </p>
          </div>
        ) : expired ? (
          <div className="text-center">
            <h1 className="text-heading-md text-default">Your session has expired</h1>
            <p className="mt-1 text-body text-subtle">Sign in to continue.</p>
          </div>
        ) : (
          <h1 className="text-center text-heading-md text-default">Sign in to Factorial</h1>
        )}
        {notice === "refused" && (
          <SectionMessage appearance="warning" title="That account can't sign in here">
            Ask an owner of this organization for an invite, or try your work account.
          </SectionMessage>
        )}
        {notice === "retry" && (
          <SectionMessage appearance="warning" title="That sign-in didn't finish">
            It took too long, or it started in another tab. Try again.
          </SectionMessage>
        )}
        <PasswordSignInForm signup={signup && !forPage && !forVerify} forgot={forgot} />
        {(google || oidc) && (
          <div className="flex items-center gap-3 text-body-sm text-subtlest">
            <span className="h-px flex-1 bg-neutral" />
            or
            <span className="h-px flex-1 bg-neutral" />
          </div>
        )}
        {/* An anchor, not a button: the browser follows the redirect chain to the IdP and back in
            this tab. Google's branding asks for its G and Roboto on the label. */}
        {google && (
          <a
            href={googleLoginUrl(next)}
            className={cx(buttonLook({ shouldFitContainer: true }), "sign-in-door")}
          >
            <GoogleG />
            Continue with Google
          </a>
        )}
        {oidc && (
          <a
            href={oidcLoginUrl(next)}
            className={cx(buttonLook({ shouldFitContainer: true }), "sign-in-door")}
          >
            Continue with {oidcLabel}
          </a>
        )}
      </div>
    </div>
  );
}

/** The ground the card sits on: the plate's dot grid in the page's own grey, and three of the
 *  mark's tiles at wall scale — two hollow, one filled — fading toward the edges. Static: the
 *  plate already carries the motion. */
function SignInGround() {
  return (
    <div
      className="sign-in-bed pointer-events-none absolute inset-0 overflow-hidden"
      aria-hidden="true"
    >
      <span className="sign-in-ghost absolute -top-16 left-[8%] h-64 w-64 rounded-md border-[14px]" />
      <span className="sign-in-ghost absolute right-[6%] -bottom-20 h-80 w-80 rounded-md border-[18px]" />
      <span className="sign-in-ghost sign-in-ghost-solid absolute top-[62%] left-[22%] h-20 w-20 rounded-md" />
    </div>
  );
}

/** Google's four-colour G, as its sign-in branding draws it. */
function GoogleG() {
  return (
    <svg viewBox="0 0 48 48" className="h-5 w-5 shrink-0" aria-hidden="true">
      <path
        fill="#EA4335"
        d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"
      />
      <path
        fill="#4285F4"
        d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"
      />
      <path
        fill="#FBBC05"
        d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"
      />
      <path
        fill="#34A853"
        d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"
      />
    </svg>
  );
}
