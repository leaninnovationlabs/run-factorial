import { describe, expect, it } from "vitest";
import { afterSignup, inviteToken, signupHref, signupInvite, slugFor } from "./accountPages";

describe("the address a new organization's name suggests", () => {
  it("is lowercase letters, digits and single dashes", () => {
    expect(slugFor("Acme Robotics, Inc.")).toBe("acme-robotics-inc");
    expect(slugFor("  --Ünïcode  Labs-- ")).toBe("unicode-labs");
  });

  it("stops at 40 characters without ending on a dash", () => {
    const slug = slugFor(`${"a".repeat(39)} b`);
    expect(slug.length).toBeLessThanOrEqual(40);
    expect(slug.endsWith("-")).toBe(false);
  });
});

describe("a pasted invite", () => {
  const token = "Zx9_Ab-cdEfGhIjKlMnOpQrStUv"; // gitleaks:allow

  it("is read from the whole link or taken as a bare token", () => {
    expect(inviteToken(`https://factory.example.com/factorial/invite/${token}`)).toBe(token);
    expect(inviteToken(`  /factorial/invite/${token}?x=1 `)).toBe(token);
    expect(inviteToken(token)).toBe(token);
  });

  it("is refused when it cannot be one", () => {
    expect(inviteToken("https://factory.example.com/factorial/orgs/acme")).toBeNull();
    expect(inviteToken("short")).toBeNull();
    expect(inviteToken("")).toBeNull();
  });
});

describe("signup reached from an invite's link", () => {
  it("carries the invite there and back", () => {
    expect(signupHref("/invite/abc_1")).toBe("/factorial/signup?next=%2Finvite%2Fabc_1");
    expect(afterSignup("?next=%2Finvite%2Fabc_1")).toBe("/factorial/invite/abc_1");
    expect(signupInvite("?next=%2Finvite%2Fabc_1")).toBe("abc_1");
  });

  it("goes to onboarding from anywhere else, and for a next that is not an invite", () => {
    expect(signupHref("/overview")).toBe("/factorial/signup");
    expect(afterSignup("")).toBe("/factorial/onboarding");
    expect(afterSignup("?next=https%3A%2F%2Fevil.example")).toBe("/factorial/onboarding");
    expect(afterSignup("?next=%2Finvite%2F..%2Forgs")).toBe("/factorial/onboarding");
    expect(signupInvite("?next=%2Finvite%2F..%2Forgs")).toBeUndefined();
  });
});
