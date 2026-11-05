/**
 * The org-less pages for the links the deployment emails (design.md §7.19 items 16 and 17):
 * the wall an unverified session meets, the page a verification link opens, and the two halves
 * of a password reset.
 *
 * A link carries its token in the fragment, which the browser never sends, so it reaches no
 * server log. Each page sends the token by POST: a mail scanner that opens the link spends
 * nothing. A verification link works only for a session of the account it was sent to, which is
 * why the app asks for a sign-in before this page sends it.
 */

import { useEffect, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import { ApiError, api } from "../api/client";
import { expireSession, useAuth } from "../auth";
import {
  Button,
  ErrorMessage,
  Form,
  SectionMessage,
  Spinner,
  TextField,
  usePageTitle,
} from "../components/ui";
import { orgless } from "../org";
import { AccountCard, PASSWORD_MIN } from "./accountPages";

/** The token in a link's fragment, or null when it holds none. */
export function fragmentToken(hash: string): string | null {
  const token = hash.replace(/^#/, "").trim();
  return /^[A-Za-z0-9_-]{20,}$/.test(token) ? token : null;
}

/** What an unverified session sees in place of every page. Opening the link in another tab
 *  verifies this session too, so returning to this one reads the session again. */
export function CheckEmailPage() {
  const { sessionEmail, refreshSession, signOut } = useAuth();
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  usePageTitle(["Verify your email"]);

  useEffect(() => {
    const again = () => {
      if (document.visibilityState !== "visible") return;
      // A link opened in another browser ends this session, which began unverified.
      refreshSession().catch((caught: unknown) => {
        if (caught instanceof ApiError && caught.status === 401) expireSession();
      });
    };
    document.addEventListener("visibilitychange", again);
    return () => document.removeEventListener("visibilitychange", again);
  }, [refreshSession]);

  async function resend() {
    setError(null);
    setBusy(true);
    try {
      await api.sendVerification();
      setSent(true);
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  return (
    <AccountCard title="Verify your email">
      <p className="text-center text-body text-subtle">
        Open the link in the email to{" "}
        <span className="font-medium text-default">{sessionEmail}</span> in this browser to
        continue. It works for a day. No email? Send the link again.
      </p>
      {sent && (
        <SectionMessage appearance="success" title="A new link is on its way">
          Only the newest link works.
        </SectionMessage>
      )}
      <ErrorMessage error={error} />
      <Button appearance="primary" shouldFitContainer isLoading={busy} onClick={resend}>
        Send the link again
      </Button>
      <p className="text-center text-body-sm text-subtle">
        Not your address?{" "}
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
      </p>
    </AccountCard>
  );
}

/** Where a verification link lands, once the browser is signed in as the account. */
export function VerifyEmailPage() {
  const { refreshSession, mustVerify } = useAuth();
  const { hash } = useLocation();
  const token = fragmentToken(hash);
  const [state, setState] = useState<"sending" | "done" | "failed">(token ? "sending" : "failed");
  const [error, setError] = useState<unknown>(null);
  const started = useRef(false);
  usePageTitle(["Verify your email"]);

  useEffect(() => {
    if (!token || started.current) return;
    started.current = true;
    api
      .verifyEmail(token)
      .then(() => refreshSession())
      .then(() => setState("done"))
      .catch((caught: unknown) => {
        setError(caught);
        setState("failed");
      });
  }, [token, refreshSession]);

  if (state === "sending") {
    return (
      <AccountCard title="Verifying your email">
        <div className="flex justify-center">
          <Spinner label="Verifying your email" />
        </div>
      </AccountCard>
    );
  }
  // An SSO sign-in verifies the account by itself, and its round trip drops the fragment.
  if (state === "done" || (!token && !mustVerify)) {
    return (
      <AccountCard title="Your email is verified">
        <p className="text-center text-body text-subtle">
          If you started in another tab, go back to it. It continues by itself.
        </p>
        <Button
          appearance="primary"
          shouldFitContainer
          onClick={() => window.location.assign(orgless("/"))}
        >
          Continue
        </Button>
      </AccountCard>
    );
  }
  return (
    <AccountCard title="This link did not work">
      {error ? (
        <ErrorMessage error={error} />
      ) : (
        <SectionMessage appearance="warning">
          The link is incomplete. Copy all of it.
        </SectionMessage>
      )}
      {mustVerify ? (
        <Button
          appearance="primary"
          shouldFitContainer
          onClick={() => window.location.assign(orgless("/"))}
        >
          Ask for a new link
        </Button>
      ) : null}
    </AccountCard>
  );
}

/** Ask for a reset link. The answer is the same whether the email has an account or not. */
export function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  usePageTitle(["Reset your password"]);

  async function submit() {
    setError(null);
    setBusy(true);
    try {
      await api.forgotPassword(email);
      setSent(true);
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  return (
    <AccountCard title="Reset your password">
      {sent ? (
        <SectionMessage appearance="success" title="Check your email">
          If an account has this email, a link is on its way to it. It works once, for an hour.
        </SectionMessage>
      ) : (
        <Form
          aria-label="Reset your password"
          validate={() => email.trim() !== ""}
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
              <ErrorMessage error={error} />
              <Button type="submit" appearance="primary" shouldFitContainer isLoading={busy}>
                Send a reset link
              </Button>
            </>
          )}
        </Form>
      )}
      <p className="text-center text-body-sm text-subtle">
        <a href={orgless("/")} className="font-medium underline">
          Back to sign in
        </a>
      </p>
    </AccountCard>
  );
}

/** Set a new password from a reset link. It signs out every other browser and signs this one
 *  in. */
export function ResetPasswordPage() {
  const { hash } = useLocation();
  const token = fragmentToken(hash);
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  usePageTitle(["Set a new password"]);

  async function submit() {
    if (!token) return;
    setError(null);
    setBusy(true);
    try {
      await api.resetPassword({ token, password });
      window.location.assign(orgless("/"));
    } catch (caught) {
      setError(caught);
      setBusy(false);
    }
  }

  if (!token) {
    return (
      <AccountCard title="This link did not work">
        <SectionMessage appearance="warning">
          The link is incomplete. Copy all of it, or ask for a new one.
        </SectionMessage>
        <p className="text-center text-body-sm text-subtle">
          <a href={orgless("/forgot-password")} className="font-medium underline">
            Ask for a new link
          </a>
        </p>
      </AccountCard>
    );
  }
  return (
    <AccountCard title="Set a new password">
      <Form
        aria-label="Set a new password"
        validate={() => password.length >= PASSWORD_MIN}
        onSubmit={submit}
        className="space-y-3"
      >
        {({ attempted }) => (
          <>
            <TextField
              label="New password"
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              helperMessage={`At least ${PASSWORD_MIN} characters. This signs you out everywhere else.`}
              errorMessage={
                attempted && password.length < PASSWORD_MIN
                  ? `Use at least ${PASSWORD_MIN} characters.`
                  : undefined
              }
            />
            <ErrorMessage error={error} />
            <Button type="submit" appearance="primary" shouldFitContainer isLoading={busy}>
              Set password
            </Button>
          </>
        )}
      </Form>
      <p className="text-center text-body-sm text-subtle">
        <a href={orgless("/forgot-password")} className="font-medium underline">
          Ask for a new link
        </a>
      </p>
    </AccountCard>
  );
}
