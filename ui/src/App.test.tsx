// @vitest-environment happy-dom
import { act } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App, PAGE_SIGN_IN_PATH, PROBE_GRACE_MS, SessionCheck } from "./App";
import { api } from "./api/client";
import { AuthProvider } from "./auth";
import { click, type Mounted, mount, settle } from "./testDom";
import { ThemeProvider } from "./theme";

let view: Mounted | null = null;
afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("SessionCheck", () => {
  it("draws nothing for a check that finishes quickly, then says it is checking", async () => {
    // Most checks answer inside the grace, and a screen that flashed on every load would be worse
    // than the blank it replaces; a slow one gets the mark and a spinner rather than nothing.
    vi.useFakeTimers();
    view = await mount(<SessionCheck />);
    expect(view.container.textContent).toBe("");
    await act(async () => {
      vi.advanceTimersByTime(PROBE_GRACE_MS);
    });
    expect(view.container.querySelector('[role="status"]')?.textContent).toContain(
      "Checking your session",
    );
    expect(view.container.querySelector('img[alt="Factorial"]')).not.toBeNull();
  });
});

describe("App", () => {
  it("shows a page link's sign-in screen whatever the reader already holds", async () => {
    // The API sends a browser here only because it has no Google session for the page; a token
    // or a dashboard session elsewhere does not read it.
    vi.spyOn(api, "authMe").mockResolvedValue({ email: "operator@leaninnovationlabs.com" });
    vi.spyOn(api, "authProviders").mockResolvedValue({
      google: true,
      oidc: false,
      oidc_label: "SSO",
    });
    // The page is a lazy chunk; a cold runner's first transform of it outlasts the settles below.
    await import("./pages/SignInPage");
    const link = "/factorial/pages/7?blob=b&expires=1&signature=s";
    view = await mount(
      <AuthProvider>
        <MemoryRouter initialEntries={[`${PAGE_SIGN_IN_PATH}?next=${encodeURIComponent(link)}`]}>
          <App />
        </MemoryRouter>
      </AuthProvider>,
    );
    await settle(50);
    await settle();

    expect(view.container.textContent).toContain("Sign in to read this page");
    expect(view.container.textContent).not.toContain("Use an API token");
    expect(view.container.querySelector('a[href*="/auth/login"]')?.getAttribute("href")).toContain(
      `/auth/login?next=${encodeURIComponent(link)}`,
    );
  });

  it("draws a reference page with no session, so make design-system needs no API", async () => {
    vi.spyOn(api, "authMe").mockRejectedValue(new Error("no API"));
    vi.spyOn(api, "authProviders").mockRejectedValue(new Error("no API"));
    await import("./pages/SamplePage");
    view = await mount(
      <AuthProvider>
        <MemoryRouter initialEntries={["/dev/sample"]}>
          <App />
        </MemoryRouter>
      </AuthProvider>,
    );
    await settle(50);
    await settle();

    expect(view.container.querySelector("h1")?.textContent).toBe("acme/widgets");
    expect(view.container.textContent).not.toContain("Sign in");
  });

  it("switches the theme from a reference page's header", async () => {
    vi.spyOn(api, "authMe").mockRejectedValue(new Error("no API"));
    vi.spyOn(api, "authProviders").mockRejectedValue(new Error("no API"));
    await import("./pages/SamplePage");
    view = await mount(
      <ThemeProvider>
        <AuthProvider>
          <MemoryRouter initialEntries={["/dev/sample"]}>
            <App />
          </MemoryRouter>
        </AuthProvider>
      </ThemeProvider>,
    );
    await settle(50);
    const theme = view.container.querySelector('[role="group"][aria-label="Theme"]');
    const dark = [...(theme?.querySelectorAll("button") ?? [])].find(
      (button) => button.textContent === "Dark",
    );
    await click(dark);

    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(dark?.getAttribute("aria-pressed")).toBe("true");
    delete document.documentElement.dataset.theme;
    localStorage.clear();
  });

  it("goes from the sample to the catalogue by the sample's own link", async () => {
    vi.spyOn(api, "authMe").mockRejectedValue(new Error("no API"));
    vi.spyOn(api, "authProviders").mockRejectedValue(new Error("no API"));
    await import("./pages/SamplePage");
    await import("./pages/CataloguePage");
    view = await mount(
      <AuthProvider>
        <MemoryRouter initialEntries={["/dev/sample"]}>
          <App />
        </MemoryRouter>
      </AuthProvider>,
    );
    await settle(50);
    const link = [...view.container.querySelectorAll("a")].find(
      (anchor) => anchor.textContent === "Catalogue",
    );
    expect(link?.getAttribute("href")).toBe("/dev/catalogue");
    await click(link as HTMLAnchorElement);
    await settle(50);
    await settle();

    expect(view.container.querySelector("h1")?.textContent).toBe("Catalogue");
  });
});
