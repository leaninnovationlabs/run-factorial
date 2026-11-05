/**
 * Who is signed in: the session cookie the API mints on a password, Google or OIDC sign-in,
 * observed through `/auth/me` (tech-stack decision 12). The cookie is HttpOnly, so this module
 * never holds a credential. Nobody signs in to the dashboard with the API token; one an older
 * version kept in `localStorage` is dropped on load, so it is never sent again.
 */

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { api } from "./api/client";
import { forgetOrganization } from "./org";
import { APP_PREFIX } from "./prefix";
import { safeSet } from "./storage";

/** Where an older dashboard kept the API token it signed in with. */
const LEGACY_TOKEN_KEY = "sf.apiToken";

interface AuthValue {
  /** The signed-in account's email, when there is a session. */
  sessionEmail: string | null;
  /** The session's email must be verified before it reaches anything (design.md §7.19 item
   *  17): the app shows only the page that says so, and the link's own page. */
  mustVerify: boolean;
  /** True while the initial session probe is in flight; render nothing rather than flash the
   * sign-in page at someone who is already signed in. */
  checking: boolean;
  /** A request came back 401 while signed in: the sign-in screen says the session expired, and
   *  renders in place, so the page asked for is still the URL once someone signs in again. */
  expired: boolean;
  /** Reads the session again after a password sign-in or a signup set its cookie. */
  refreshSession: () => Promise<void>;
  signOut: () => void;
  /** Ends the session and shows the sign-in screen in place, so the URL is still this page: a
   *  password sign-in comes back to it, and so does each SSO door the install offers. Not a link
   *  to one provider: on a password-only install that link is a 404. */
  signInAgain: () => Promise<void>;
}

const AuthContext = createContext<AuthValue | null>(null);

const expiryListeners = new Set<() => void>();

/**
 * The session has lapsed. Called by the query client for every 401 (`queryClient.ts`)
 * — outside React, so it reaches the provider through a listener the provider registers.
 */
export function expireSession(): void {
  for (const listener of expiryListeners) listener();
}

safeSet(LEGACY_TOKEN_KEY, null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [sessionEmail, setSessionEmail] = useState<string | null>(null);
  const [mustVerify, setMustVerify] = useState(false);
  const [checking, setChecking] = useState(true);
  const [expired, setExpired] = useState(false);

  useEffect(() => {
    const expire = () => {
      setSessionEmail(null);
      setExpired(true);
    };
    expiryListeners.add(expire);
    return () => {
      expiryListeners.delete(expire);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    api
      .authMe()
      .then((me) => {
        if (cancelled) return;
        setSessionEmail(me.email);
        setMustVerify(me.must_verify === true);
      })
      .catch(() => {
        // 401 (no session), or an API that predates /auth — either way, no session.
      })
      .finally(() => {
        if (!cancelled) setChecking(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const refreshSession = useCallback(async () => {
    const me = await api.authMe();
    setSessionEmail(me.email);
    setMustVerify(me.must_verify === true);
    setExpired(false);
  }, []);

  const signOut = useCallback(() => {
    setSessionEmail(null);
    setExpired(false);
    // The next person to sign in on this browser starts from their own organization, not the
    // one this person used last.
    forgetOrganization();
    // Navigate so the API can clear the cookie and send the browser to the IdP logout.
    // A POST-only logout left Okta signed in, so Continue with Okta skipped the password.
    window.location.assign(`${APP_PREFIX}/auth/logout`);
  }, []);

  const signInAgain = useCallback(async () => {
    // A session the API already dropped answers 401 here, which changes nothing.
    await api.authLogout().catch(() => undefined);
    setSessionEmail(null);
    setExpired(false);
  }, []);

  const value = useMemo(
    () => ({ sessionEmail, mustVerify, checking, expired, refreshSession, signOut, signInAgain }),
    [sessionEmail, mustVerify, checking, expired, refreshSession, signOut, signInAgain],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (value === null) throw new Error("useAuth must be used inside <AuthProvider>");
  return value;
}
