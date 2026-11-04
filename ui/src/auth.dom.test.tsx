// @vitest-environment happy-dom
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api/client";
import { AuthProvider, useAuth } from "./auth";
import { CloseCode } from "./components/terminal/protocol";
import { StoppedMessage } from "./components/terminal/TerminalTab";
import { SignInAgain } from "./pages/McpAccessPage";
import { click, type Mounted, mount, settle } from "./testDom";

let view: Mounted | null = null;
afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.restoreAllMocks();
});

function Session({ children }: { children: ReactNode }) {
  const { sessionEmail } = useAuth();
  return sessionEmail ? children : <p>Sign in to Factorial</p>;
}

const signedOut = {
  phase: "stopped",
  kind: "signed-out",
  code: CloseCode.SIGNED_OUT,
  reason: "Your session has ended. Sign in again to use the terminal.",
} as const;

describe.each([
  [
    "the MCP page",
    <SignInAgain key="mcp" isChecking={false} onCheckAgain={() => {}} onCancel={() => {}} />,
  ],
  ["the terminal", <StoppedMessage key="terminal" state={signedOut} onRetry={() => {}} />],
])("Sign in again on %s", (_, prompt) => {
  it("ends the session in place rather than linking to a provider the install may lack", async () => {
    // A password-only install has no Google sign-in, and its /auth/login is a 404.
    vi.spyOn(api, "authMe").mockResolvedValue({ email: "operator@example.com" });
    const logout = vi.spyOn(api, "authLogout").mockResolvedValue(undefined);
    view = await mount(
      <AuthProvider>
        <Session>{prompt}</Session>
      </AuthProvider>,
    );
    await settle();
    expect(view.container.querySelector('a[href*="/auth/"]')).toBeNull();

    const button = [...view.container.querySelectorAll("button")].find(
      (b) => b.textContent === "Sign in again",
    );
    await click(button);
    await settle();

    expect(logout).toHaveBeenCalledOnce();
    expect(view.container.textContent).toContain("Sign in to Factorial");
  });
});
