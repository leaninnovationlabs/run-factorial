import { type ComponentType, lazy, Suspense, useEffect, useState } from "react";
import { Route, Routes, useLocation } from "react-router-dom";
import logoUrl from "../logo.svg";
import { useAuth } from "./auth";
import { AppShell } from "./components/shell/AppShell";
import { OrganizationGate } from "./components/shell/OrganizationGate";
import { Spinner } from "./components/ui";

/** The reference pages (docs/design-system.md), ahead of the session check (decision 50). In
 *  development builds only: the condition is a build-time constant, so a production bundle drops
 *  the imports. */
const REFERENCE_ROUTES: Record<string, ComponentType> = import.meta.env.DEV
  ? {
      "/dev/guide": lazy(() => import("./pages/GuidePage")),
      "/dev/catalogue": lazy(() => import("./pages/CataloguePage")),
      "/dev/sample": lazy(() => import("./pages/SamplePage")),
    }
  : {};

/** A signed-in reader never needs the sign-in page, so it is a chunk of its own too. */
const SignInPage = lazy(() =>
  import("./pages/SignInPage").then((m) => ({ default: m.SignInPage })),
);

/** The org-less account pages (design.md §7.19), one chunk: most readers never see them. */
const SignUpPage = lazy(() =>
  import("./pages/accountPages").then((m) => ({ default: m.SignUpPage })),
);
const OnboardingPage = lazy(() =>
  import("./pages/accountPages").then((m) => ({ default: m.OnboardingPage })),
);
const InvitePage = lazy(() =>
  import("./pages/accountPages").then((m) => ({ default: m.InvitePage })),
);

/** The pages the deployment's emails link to (design.md §7.19 items 16 and 17), one chunk. */
const CheckEmailPage = lazy(() =>
  import("./pages/emailPages").then((m) => ({ default: m.CheckEmailPage })),
);
const VerifyEmailPage = lazy(() =>
  import("./pages/emailPages").then((m) => ({ default: m.VerifyEmailPage })),
);
const ForgotPasswordPage = lazy(() =>
  import("./pages/emailPages").then((m) => ({ default: m.ForgotPasswordPage })),
);
const ResetPasswordPage = lazy(() =>
  import("./pages/emailPages").then((m) => ({ default: m.ResetPasswordPage })),
);

/** Where a verification link lands. Only a session of its account redeems it, so a browser
 *  without one signs in here first and the page sends the token after. */
export const VERIFY_EMAIL_PATH = "/verify-email";

/** Pages that need no session: a reset link proves the mailbox by itself. */
const SESSIONLESS: Record<string, ComponentType> = {
  "/forgot-password": ForgotPasswordPage,
  "/reset-password": ResetPasswordPage,
};

/** Where a step's page link sends a browser that has no session (`api/routes/pages.py`). */
export const PAGE_SIGN_IN_PATH = "/sign-in";

/** How long the session check may run before the wait is shown. Most finish sooner, and a screen
 *  that flashed on every load would be worse than the brief blank it replaces. */
export const PROBE_GRACE_MS = 150;

/** While the session is being checked: nothing at first, then the mark and a spinner. */
export function SessionCheck() {
  const [late, setLate] = useState(false);
  useEffect(() => {
    const timer = window.setTimeout(() => setLate(true), PROBE_GRACE_MS);
    return () => window.clearTimeout(timer);
  }, []);
  if (!late) return null;
  return (
    <div className="flex h-dvh flex-col items-center justify-center gap-4 bg-surface-sunken">
      <img data-nav-mark src={logoUrl} alt="Factorial" className="size-10" />
      <Spinner label="Checking your session" delay={0} />
    </div>
  );
}

export function App() {
  const { sessionEmail, mustVerify, checking, signOut } = useAuth();
  const { pathname } = useLocation();

  // Shown signed in or not: the page moves a signed-in reader on to the page link. The link
  // refuses an unverified session too, so moving that one on would loop.
  if (pathname === PAGE_SIGN_IN_PATH) {
    return (
      <Suspense fallback={<SessionCheck />}>
        {sessionEmail && mustVerify ? <CheckEmailPage /> : <SignInPage forPage />}
      </Suspense>
    );
  }
  const ReferencePage = REFERENCE_ROUTES[pathname];
  if (ReferencePage) {
    return (
      <Suspense fallback={null}>
        <ReferencePage />
      </Suspense>
    );
  }
  const Sessionless = SESSIONLESS[pathname];
  if (Sessionless) {
    return (
      <Suspense fallback={<SessionCheck />}>
        <Sessionless />
      </Suspense>
    );
  }
  // One gate: a session, from a password or an SSO sign-in.
  if (checking) return <SessionCheck />;
  if (!sessionEmail) {
    return (
      <Suspense fallback={<SessionCheck />}>
        {pathname === "/signup" ? (
          <SignUpPage />
        ) : (
          <SignInPage forVerify={pathname === VERIFY_EMAIL_PATH} />
        )}
      </Suspense>
    );
  }
  // The second gate, while the deployment sends mail: a verified email.
  if (pathname === VERIFY_EMAIL_PATH || mustVerify) {
    return (
      <Suspense fallback={<SessionCheck />}>
        {pathname === VERIFY_EMAIL_PATH ? <VerifyEmailPage /> : <CheckEmailPage />}
      </Suspense>
    );
  }

  return (
    <Routes>
      <Route
        path="/signup"
        element={
          <Suspense fallback={<SessionCheck />}>
            <OnboardingPage />
          </Suspense>
        }
      />
      <Route
        path="/onboarding"
        element={
          <Suspense fallback={<SessionCheck />}>
            <OnboardingPage />
          </Suspense>
        }
      />
      <Route
        path="/invite/:token"
        element={
          <Suspense fallback={<SessionCheck />}>
            <InvitePage />
          </Suspense>
        }
      />
      <Route
        path="*"
        element={
          <OrganizationGate>
            <AppShell sessionEmail={sessionEmail} onSignOut={signOut} />
          </OrganizationGate>
        }
      />
    </Routes>
  );
}
