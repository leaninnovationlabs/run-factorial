// @vitest-environment happy-dom
import { act } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../api/client";
import { AuthProvider } from "../auth";
import { type Mounted, mount, settle } from "../testDom";
import { PROVIDERS_RETRY_MS, SignInPage } from "./SignInPage";

let view: Mounted | null = null;
afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("SignInPage", () => {
  it("offers Google once the API answers, when it first met a deploy's 503", async () => {
    // A stop-first deploy leaves no task behind the ALB for minutes; a page loaded then must not
    // stay without its Google door until someone thinks to reload it.
    vi.useFakeTimers();
    vi.spyOn(api, "authMe").mockRejectedValue(new ApiError(401, "signed out"));
    const providers = vi
      .spyOn(api, "authProviders")
      .mockRejectedValueOnce(new ApiError(503, "Service Temporarily Unavailable"))
      .mockResolvedValue({ google: true, oidc: false, oidc_label: "SSO" });
    view = await mount(
      <AuthProvider>
        <MemoryRouter>
          <SignInPage />
        </MemoryRouter>
      </AuthProvider>,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(view.container.textContent).not.toContain("Continue with Google");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PROVIDERS_RETRY_MS);
    });
    expect(providers).toHaveBeenCalledTimes(2);
    expect(view.container.textContent).toContain("Continue with Google");
  });

  it("stops asking when the API refuses the route", async () => {
    vi.useFakeTimers();
    vi.spyOn(api, "authMe").mockRejectedValue(new ApiError(401, "signed out"));
    const providers = vi
      .spyOn(api, "authProviders")
      .mockRejectedValue(new ApiError(404, "Not Found"));
    view = await mount(
      <AuthProvider>
        <MemoryRouter>
          <SignInPage />
        </MemoryRouter>
      </AuthProvider>,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PROVIDERS_RETRY_MS * 3);
    });
    expect(providers).toHaveBeenCalledTimes(1);
  });

  it("sends a reader signed in on the page-link screen on to the page", async () => {
    // An SSO sign-in comes back to the link by itself; a password sign-in only reads the session.
    const LINK = "/factorial/pages/7?blob=b&expires=1&signature=s";
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    vi.spyOn(api, "authMe").mockResolvedValue({ email: "operator@example.com" });
    vi.spyOn(api, "authProviders").mockResolvedValue({
      google: false,
      oidc: false,
      oidc_label: "",
    });
    try {
      view = await mount(
        <AuthProvider>
          <MemoryRouter initialEntries={[`/sign-in?next=${encodeURIComponent(LINK)}`]}>
            <SignInPage forPage />
          </MemoryRouter>
        </AuthProvider>,
      );
      await settle();
      expect(assign).toHaveBeenCalledWith(LINK);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
