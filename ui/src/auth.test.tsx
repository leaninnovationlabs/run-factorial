// @vitest-environment happy-dom
/**
 * The auth provider: a session is the one way in. A lapsed one is marked EXPIRED rather than
 * read as nobody ever signing in, so the sign-in screen can say so; a token an older dashboard
 * stored is dropped, never sent; and signing out forgets the organization this browser used.
 */

import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type Mounted, mount, settle } from "./testDom";

let view: Mounted | null = null;

beforeEach(() => {
  vi.resetModules();
  window.localStorage.setItem("sf.apiToken", "stale-token");
  window.localStorage.setItem("sf-ui:org", "acme");
});

afterEach(async () => {
  await view?.unmount();
  view = null;
  window.localStorage.clear();
  vi.restoreAllMocks();
});

async function provider() {
  // Imported after the storage is seeded: the module drops the old token when it loads.
  const { api } = await import("./api/client");
  vi.spyOn(api, "authMe").mockResolvedValue({ email: "operator@example.com" });
  const auth = await import("./auth");
  let signOut: () => void = () => {};
  function Probe() {
    const value = auth.useAuth();
    signOut = value.signOut;
    return <output>{`${value.sessionEmail ?? "none"}|${value.expired}`}</output>;
  }
  view = await mount(
    <auth.AuthProvider>
      <Probe />
    </auth.AuthProvider>,
  );
  await settle();
  const container = view.container;
  return {
    auth,
    read: () => container.querySelector("output")?.textContent,
    signOut: () => signOut(),
  };
}

describe("the session", () => {
  it("drops a token an older dashboard stored", async () => {
    await provider();
    expect(window.localStorage.getItem("sf.apiToken")).toBeNull();
  });

  it("is marked expired when a request says it lapsed", async () => {
    const { auth, read } = await provider();
    expect(read()).toBe("operator@example.com|false");
    await act(async () => auth.expireSession());
    expect(read()).toBe("none|true");
  });

  it("forgets the remembered organization on sign-out", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    try {
      const { signOut } = await provider();
      await act(async () => signOut());
      expect(window.localStorage.getItem("sf-ui:org")).toBeNull();
      expect(assign).toHaveBeenCalledWith("/factorial/auth/logout");
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
