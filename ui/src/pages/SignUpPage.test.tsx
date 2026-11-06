/**
 * The Create account card's SSO doors, rendered with `renderToString` as the sign-in card's tests
 * are. Nothing pinned them before, which is how the two pages' Google buttons drifted apart.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import type { AuthProviders } from "../api/client";
import { keys } from "../api/keys";
import { AuthProvider } from "../auth";
import { SignUpPage } from "./accountPages";

function signUp(over: Partial<AuthProviders> = {}): string {
  const client = new QueryClient();
  client.setQueryData(keys.authProviders, {
    google: true,
    oidc: false,
    oidc_label: "",
    password: true,
    signup: true,
    multi_org: true,
    ...over,
  });
  return renderToString(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/signup"]}>
        <AuthProvider>
          <SignUpPage />
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("SignUpPage", () => {
  it("offers Google's own button, with its G, after the form", () => {
    const html = signUp();
    expect(html).toContain("Sign up with Google");
    expect(html).toMatch(/<a[^>]*sign-in-door[^>]*><img/);
    expect(html).toMatch(/href="[^"]*\/auth\/login\?next=/);
    expect(html.indexOf('aria-label="Create an account"')).toBeLessThan(
      html.indexOf("Sign up with Google"),
    );
  });

  it("names the OIDC issuer in the same shape", () => {
    const html = signUp({ google: false, oidc: true, oidc_label: "Okta" });
    expect(html).toContain("Sign up with Okta");
    expect(html.match(/sign-in-door/g)).toHaveLength(1);
  });

  it("shows no door and no divider where no SSO is set up", () => {
    const html = signUp({ google: false });
    expect(html).not.toContain("sign-in-door");
    expect(html).not.toMatch(/>or</);
  });
});
