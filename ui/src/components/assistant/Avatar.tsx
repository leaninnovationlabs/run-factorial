/**
 * Who spoke, at the head of each block of the conversation: the person by the initials of the
 * address they signed in with, the assistant by the Factorial mark. Both are decorative — the
 * stream names the speaker in words for a screen reader — and a token sign-in, which carries no
 * address, gets the person glyph.
 */

import logoUrl from "../../../logo.svg";
import { useAuth } from "../../auth";
import { Icon } from "../ui";

/** "first.last@…" is "FL": the first letters of the address's name parts, at most two. */
export function initials(email: string): string {
  const name = email.split("@")[0] ?? "";
  return name
    .split(/[._+-]/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part.charAt(0).toUpperCase())
    .join("");
}

export function PersonAvatar() {
  const { sessionEmail } = useAuth();
  const letters = sessionEmail ? initials(sessionEmail) : "";
  return (
    <span
      aria-hidden="true"
      className="flex size-6 shrink-0 items-center justify-center rounded-full border border-default bg-neutral text-body-sm font-semibold text-subtle"
    >
      {letters || <Icon glyph="user" size="small" />}
    </span>
  );
}

export function AssistantAvatar() {
  return <img data-nav-mark src={logoUrl} alt="" className="size-6 shrink-0 rounded-md" />;
}
