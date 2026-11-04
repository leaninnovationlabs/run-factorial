/**
 * Which organization a page acts in (tech-stack decision 68), read from its URL before the first
 * render.
 *
 * Every signed-in page lives under `/factorial/orgs/<slug>`, and that is the router's `basename`. So
 * the app's own links (`/projects`, `/tickets/7`) and the routes they match stay as they were, and
 * resolve inside the organization the reader is in. The organization is fixed for the page's
 * life: moving to another one is a full load, so no cached query can answer for the wrong tenant.
 *
 * A bare path, from a bookmark or a link written before organizations, is moved under the last
 * organization this browser used, or `default`, before the router sees it. Some paths belong to no
 * organization: a step page's sign-in (`/sign-in`), the development reference pages, and, with
 * multi-org on, signup, onboarding and an invite's link (design.md §7.19).
 */

import { createContext, type ReactNode, useContext } from "react";
import { APP_PREFIX } from "./prefix";
import { safeGet, safeSet } from "./storage";

export const ORG_STORAGE_KEY = "sf-ui:org";
export const DEFAULT_ORG = "default";
/** `api.deps.organization_path`'s pattern, which a remembered slug must match to be used. */
const SLUG = /^[a-z0-9][a-z0-9-]{0,62}$/;
const ORGLESS: readonly string[] = [
  "/sign-in",
  "/dev/guide",
  "/dev/catalogue",
  "/dev/sample",
  "/signup",
  "/onboarding",
];
const INVITE = /^\/invite\/[^/]+$/;

/** Whether a path, below the app's prefix, belongs to no organization. */
export function isOrgless(path: string): boolean {
  return ORGLESS.includes(path) || INVITE.test(path);
}

export type Entry = {
  /** The organization the page acts in; null on an org-less path. */
  org: string | null;
  /** Where to move the URL before the first render, when it named no organization. */
  redirect: string | null;
};

export function enter(
  location: { pathname: string; search: string; hash: string },
  remembered: string | null,
): Entry {
  const path = inApp(location.pathname);
  const named = /^\/orgs(?:\/([^/]*))?(?:\/|$)/.exec(path);
  const slug = named?.[1] ? decoded(named[1]) : null;
  if (slug) return { org: slug, redirect: null };
  if (isOrgless(path)) return { org: null, redirect: null };
  const org = remembered && SLUG.test(remembered) ? remembered : DEFAULT_ORG;
  // `/orgs` with no slug, or one no URL could hold, goes to the organization's home.
  const rest = path === "/" || named ? "" : path;
  return { org, redirect: `${basename(org)}${rest}${location.search}${location.hash}` };
}

/** `pathname` below the app's prefix; the server sends anything outside it under it. */
function inApp(pathname: string): string {
  const rest = pathname.startsWith(APP_PREFIX) ? pathname.slice(APP_PREFIX.length) : pathname;
  return rest.startsWith("/") ? rest : `/${rest}`;
}

/** Null for a malformed escape, which would otherwise throw before the first render. */
function decoded(segment: string): string | null {
  try {
    return decodeURIComponent(segment);
  } catch {
    return null;
  }
}

export function basename(org: string): string {
  return `${APP_PREFIX}/orgs/${encodeURIComponent(org)}`;
}

/** The organization a bare path moves into next time: set once the page's slug is known real. */
export function rememberOrganization(org: string): void {
  safeSet(ORG_STORAGE_KEY, org);
}

/** Sign-out forgets it: the next person on this browser starts from their own organization. */
export function forgetOrganization(): void {
  safeSet(ORG_STORAGE_KEY, null);
}

export function rememberedOrganization(): string | null {
  return safeGet(ORG_STORAGE_KEY);
}

const OrgContext = createContext<string>(DEFAULT_ORG);

export function OrgProvider({ org, children }: { org: string; children: ReactNode }) {
  return <OrgContext.Provider value={org}>{children}</OrgContext.Provider>;
}

/** The slug of the organization this page acts in. */
export function useOrg(): string {
  return useContext(OrgContext);
}

/** A router path as the browser addresses it, for the places that leave the router: a sign-in's
 *  return path is followed by the API, which knows nothing of the basename. */
export function inOrganization(org: string, path: string): string {
  return `${basename(org)}${path === "/" ? "" : path}`;
}

/** An org-less page as the browser addresses it. A full load moves there: the router's basename
 *  is fixed for the page's life. */
export function orgless(path: string): string {
  return `${APP_PREFIX}${path}`;
}
