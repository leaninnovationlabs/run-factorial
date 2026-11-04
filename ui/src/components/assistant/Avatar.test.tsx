// @vitest-environment happy-dom
/** Who spoke: the person by their initials, the assistant by the Factorial mark. */

import { afterEach, describe, expect, test, vi } from "vitest";
import { ApiError, api } from "../../api/client";
import { AuthProvider } from "../../auth";
import { type Mounted, mount, settle } from "../../testDom";
import { AssistantAvatar, initials, PersonAvatar } from "./Avatar";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.restoreAllMocks();
});

describe("initials", () => {
  test("takes the first letters of the address's name parts, at most two", () => {
    expect(initials("first.last@example.com")).toBe("FL");
    expect(initials("teammate@example.com")).toBe("T");
    expect(initials("operator_b-c+d@example.com")).toBe("OB");
    expect(initials(".x@example.com")).toBe("X");
  });
});

describe("PersonAvatar", () => {
  test("shows the signed-in person's initials", async () => {
    vi.spyOn(api, "authMe").mockResolvedValue({ email: "operator@example.com" });
    view = await mount(
      <AuthProvider>
        <PersonAvatar />
      </AuthProvider>,
    );
    await settle();
    expect(view.container.textContent).toBe("O");
    expect(view.container.firstElementChild?.getAttribute("aria-hidden")).toBe("true");
  });

  test("shows the person glyph when the sign-in carries no address", async () => {
    vi.spyOn(api, "authMe").mockRejectedValue(new ApiError(401, "no session"));
    view = await mount(
      <AuthProvider>
        <PersonAvatar />
      </AuthProvider>,
    );
    await settle();
    expect(view.container.textContent).toBe("");
    expect(view.container.querySelector('[data-glyph="user"]')).not.toBeNull();
  });
});

describe("AssistantAvatar", () => {
  test("is the Factorial mark, decorative", async () => {
    view = await mount(<AssistantAvatar />);
    const mark = view.container.querySelector("img");
    expect(mark?.getAttribute("alt")).toBe("");
    expect(mark?.hasAttribute("data-nav-mark")).toBe(true);
  });
});
