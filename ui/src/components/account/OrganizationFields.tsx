/**
 * A new organization's name and address, and the call that makes it: one form behind onboarding's
 * card and the page that creates an organization from inside one. The address follows the name
 * until the person edits it. Making one is a full load of its overview: the organization is the
 * router's basename (`org.tsx`).
 */

import { useState } from "react";
import { api } from "../../api/client";
import { inOrganization } from "../../org";
import { TextField } from "../ui";

/** A slug from a name, as the form suggests it: what `POST /api/orgs` accepts, or "". */
export function slugFor(name: string): string {
  return (
    name
      .toLowerCase()
      .normalize("NFKD")
      // The accents NFKD splits off, so "é" becomes "e" rather than a dash.
      .replace(/[\u0300-\u036f]/g, "")
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 40)
      .replace(/-+$/, "")
  );
}

export type NewOrganization = ReturnType<typeof useNewOrganization>;

export function useNewOrganization() {
  const [name, setName] = useState("");
  const [typedSlug, setTypedSlug] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const slug = typedSlug ?? slugFor(name);

  async function submit() {
    setError(null);
    setBusy(true);
    try {
      const created = await api.createOrganization({ name, slug });
      window.location.assign(inOrganization(created.slug, "/overview"));
    } catch (caught) {
      setError(caught);
      setBusy(false);
    }
  }

  return {
    name,
    setName,
    slug,
    setSlug: (typed: string) => setTypedSlug(typed.toLowerCase()),
    valid: name.trim() !== "" && slug.length >= 3,
    error,
    busy,
    submit,
  };
}

export function OrganizationFields({
  draft,
  attempted,
  isRequired = false,
}: {
  draft: NewOrganization;
  attempted: boolean;
  isRequired?: boolean;
}) {
  return (
    <>
      <TextField
        label="Name"
        isRequired={isRequired}
        value={draft.name}
        onChange={(event) => draft.setName(event.target.value)}
        errorMessage={attempted && draft.name.trim() === "" ? "Name the organization." : undefined}
      />
      <TextField
        label="Address"
        isMonospaced
        isRequired={isRequired}
        value={draft.slug}
        onChange={(event) => draft.setSlug(event.target.value)}
        helperMessage="Used in every link to the organization. You can't change it later."
        errorMessage={attempted && draft.slug.length < 3 ? "Use at least 3 characters." : undefined}
      />
    </>
  );
}
