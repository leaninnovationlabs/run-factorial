import { describe, expect, it } from "vitest";
import { fragmentToken } from "./emailPages";

describe("the token in an emailed link", () => {
  const token = "Zx9_Ab-cdEfGhIjKlMnOpQrStUvWxYz0123456789ab"; // gitleaks:allow

  it("is the fragment, with or without its hash", () => {
    expect(fragmentToken(`#${token}`)).toBe(token);
    expect(fragmentToken(token)).toBe(token);
  });

  it("is refused when the fragment cannot be one", () => {
    expect(fragmentToken("")).toBeNull();
    expect(fragmentToken("#short")).toBeNull();
    expect(fragmentToken(`#${token}&x=1`)).toBeNull();
  });
});
