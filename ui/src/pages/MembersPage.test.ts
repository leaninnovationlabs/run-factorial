import { describe, expect, it } from "vitest";
import type { InviteCreated, Member, Role } from "../api/types";
import { inviteLink } from "../components/account/InviteLink";
import { grantable, inviterName, roleGrid } from "./MembersPage";

const role = (key: string, permissions: string[]): Role => ({
  key,
  name: key,
  description: "",
  permissions,
});

describe("the roles a reader may grant", () => {
  const roles = [
    role("owner", ["org:read", "org:manage", "members:invite"]),
    role("admin", ["org:read", "members:invite"]),
    role("member", ["org:read"]),
  ];

  it("are those holding nothing the reader lacks", () => {
    expect(grantable(roles, ["org:read", "members:invite"]).map((r) => r.key)).toEqual([
      "admin",
      "member",
    ]);
    expect(grantable(roles, []).map((r) => r.key)).toEqual([]);
  });
});

describe("what each role can do", () => {
  const roles = [role("owner", ["org:read", "org:manage"]), role("member", ["org:read"])];
  const permissions = [
    { key: "org:read", description: "See it" },
    { key: "org:manage", description: "Change it" },
  ];

  it("marks, for every permission in the catalogue's order, the roles holding it", () => {
    expect(roleGrid(permissions, roles)).toEqual([
      { ...permissions[0], held: { owner: true, member: true } },
      { ...permissions[1], held: { owner: true, member: false } },
    ]);
  });
});

describe("an invite's link", () => {
  const created = {
    id: 1,
    email: "a@example.com",
    role: "member",
    created_by: "o@example.com",
    created_at: "2026-10-24T00:00:00Z",
    expires_at: "2026-10-31T00:00:00Z",
    path: "/factorial/invite/tok",
  };

  it("is the API's own URL when the deployment names one", () => {
    const invite: InviteCreated = { ...created, url: "https://f.example.com/factorial/invite/tok" };
    expect(inviteLink(invite, "http://localhost:5173")).toBe(invite.url);
  });

  it("is the path on this page's origin otherwise", () => {
    expect(inviteLink({ ...created, url: null }, "http://localhost:5173")).toBe(
      "http://localhost:5173/factorial/invite/tok",
    );
  });
});

describe("who sent an invite", () => {
  const members: Member[] = [
    {
      user_id: 1,
      email: "o@example.com",
      name: "Olive Owner",
      role: "owner",
      verified: true,
      added_by: "o@example.com",
      created_at: "2026-10-24T00:00:00Z",
    },
  ];

  it("is a member's name, or the address once they are gone", () => {
    expect(inviterName("o@example.com", members)).toBe("Olive Owner");
    expect(inviterName("gone@example.com", members)).toBe("gone@example.com");
  });

  it("is the API token for an invite made on it", () => {
    expect(inviterName("api-token", members)).toBe("API token");
  });
});
