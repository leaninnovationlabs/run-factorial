/**
 * The card's shapes, rendered with `renderToString` as the page tests are: no DOM, no fetch,
 * first paint only. Which SSO doors show is decided by `/auth/providers` at runtime; here it is a
 * prop, because the effect that asks never runs under `renderToString`.
 */

import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { AuthProvider } from "../auth";
import { pageLink, SignInCard, signInNotice, withoutNotice } from "./SignInPage";

function card(
  google: boolean,
  over: {
    expired?: boolean;
    next?: string;
    forPage?: boolean;
    notice?: "refused" | "retry";
    oidc?: boolean;
    oidcLabel?: string;
    signup?: boolean;
    at?: string;
  } = {},
): string {
  return renderToString(
    <MemoryRouter initialEntries={[over.at ?? "/"]}>
      <AuthProvider>
        <SignInCard
          google={google}
          oidc={over.oidc}
          oidcLabel={over.oidcLabel}
          signup={over.signup}
          expired={over.expired ?? false}
          next={over.next ?? "/"}
          forPage={over.forPage}
          notice={over.notice}
        />
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("SignInCard", () => {
  it("offers the password form and the configured SSO, and never the API token", () => {
    // Nobody signs in to the dashboard with the API token (design.md §7.19).
    const html = card(true);
    expect(html).toContain('aria-label="Sign in with a password"');
    expect(html).toContain("Sign in with Google");
    expect(html).toMatch(/<a[^>]*sign-in-door[^>]*><img/);
    expect(html).not.toContain("API token");
    expect(html).not.toContain("FACTORIAL_API_TOKEN");
  });

  it("is the password form alone where no SSO is configured", () => {
    const html = card(false);
    expect(html).toContain('aria-label="Sign in with a password"');
    expect(html).not.toContain("sign-in-door");
    expect(html).not.toMatch(/>or</);
  });

  it("offers the configured OIDC issuer", () => {
    const html = card(false, { oidc: true, oidcLabel: "Okta" });
    expect(html).toContain("Sign in with Okta");
    expect(html).toContain("/auth/oidc/login");
  });

  it("offers signing up where the deployment allows it, and on an invite's link", () => {
    expect(card(false, { signup: true })).toContain("Create an account");
    expect(card(false)).not.toContain("Create an account");
    // An invite's email may sign up whatever the deployment allows strangers.
    expect(card(false, { at: "/invite/tok" })).toContain("Create an account");
  });

  it("carries the mark beside the doors", () => {
    expect(card(true)).toContain('aria-label="Factorial"');
  });

  it("centres the heading over the doors", () => {
    expect(card(true)).toMatch(/<h1[^>]*text-center/);
  });

  it("says the session expired when that is why it is showing", () => {
    // Reached through a 401 on a page someone was using, not by arriving signed out.
    const html = card(true, { expired: true });
    expect(html).toContain("Your session has expired");
    expect(html).toContain("Sign in to continue.");
    expect(card(true)).not.toContain("expired");
  });

  it("sends Google sign-in back to the page that asked for it", () => {
    // The callback lands on `/` unless it is told otherwise, which lost the ticket the reader
    // was on every time a session lapsed.
    expect(card(true, { next: "/tickets/204" })).toMatch(
      /href="[^"]*\/auth\/login\?next=%2Ftickets%2F204"/,
    );
    expect(card(true, { next: "/" })).toMatch(/href="[^"]*\/auth\/login"/);
  });

  it("sets every SSO door in the same face", () => {
    // Google's branding sets its button's size, colours and face; every door wears the same class
    // so they stay one shape in every skin.
    expect(card(true, { oidc: true }).match(/sign-in-door/g)).toHaveLength(2);
  });
});

describe("signing in to read a step's page", () => {
  const LINK = "/factorial/pages/7?blob=b&expires=1&signature=s";

  it("offers the same doors under its own heading, and returns to the page", () => {
    const html = card(true, { forPage: true, next: LINK, signup: true });
    expect(html).toContain("Sign in to read this page");
    expect(html).toContain('aria-label="Sign in with a password"');
    expect(html).toContain(`/auth/login?next=${encodeURIComponent(LINK)}`);
    expect(html).not.toContain("Create an account");
  });

  it("returns only to a page link, whatever else `next` names", () => {
    expect(pageLink(`?next=${encodeURIComponent(LINK)}`)).toBe(LINK);
    for (const other of [
      "https://evil.example/factorial/pages/1",
      "//evil.example/factorial/pages/1",
      "/factorialx/pages/1",
      "/pages/1",
      "/factorial/tickets/2",
    ]) {
      expect(pageLink(`?next=${encodeURIComponent(other)}`)).toBe("/");
    }
    expect(pageLink("")).toBe("/");
  });
});

describe("coming back from a Google sign-in that did not finish", () => {
  it("says the account cannot sign in, and keeps the Google door to choose another", () => {
    const html = card(true, { notice: "refused" });
    expect(html).toContain("That account can&#x27;t sign in here");
    expect(html).toContain("Sign in with Google");
  });

  it("reads only the two words the callback sends", () => {
    // The query is anyone's to write: nothing in it is shown but these two fixed messages.
    expect(signInNotice("?signin=refused")).toBe("refused");
    expect(signInNotice("?signin=retry")).toBe("retry");
    expect(signInNotice("?signin=<b>you%20won</b>")).toBeNull();
    expect(signInNotice("")).toBeNull();
  });

  it("returns to the same route without the failure word", () => {
    expect(withoutNotice({ pathname: "/", search: "?signin=refused" })).toBe("/");
    expect(withoutNotice({ pathname: "/tickets/2", search: "?tab=activity&signin=retry" })).toBe(
      "/tickets/2?tab=activity",
    );
  });
});
