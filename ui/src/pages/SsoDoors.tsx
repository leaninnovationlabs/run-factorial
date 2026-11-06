/**
 * The SSO doors under a sign-in or sign-up form: an "or" rule, then Google's button, then the
 * deployment's OIDC issuer in the same shape. One component for both cards, because two copies
 * drifted apart. Google's button follows its branding guidelines, not the dashboard's Button;
 * `signIn.css` draws it.
 */

import { googleLoginUrl, oidcLoginUrl } from "../api/client";
import googleG from "./google-g.png";

type SsoDoorsProps = {
  intent: "sign-in" | "sign-up";
  /** The path to come back to after the round trip. */
  next: string;
  google: boolean;
  oidc: boolean;
  oidcLabel: string;
};

export function SsoDoors({ intent, next, google, oidc, oidcLabel }: SsoDoorsProps) {
  if (!google && !oidc) return null;
  const verb = intent === "sign-up" ? "Sign up with" : "Sign in with";
  return (
    <>
      <div className="flex items-center gap-3 text-body-sm text-subtlest">
        <span className="h-px flex-1 bg-neutral" />
        or
        <span className="h-px flex-1 bg-neutral" />
      </div>
      {/* Anchors, not buttons: the browser follows the redirect chain to the IdP and back. */}
      {google && (
        <a href={googleLoginUrl(next)} className="sign-in-door">
          <img src={googleG} alt="" width={20} height={20} />
          {`${verb} Google`}
        </a>
      )}
      {oidc && (
        <a href={oidcLoginUrl(next)} className="sign-in-door">
          {`${verb} ${oidcLabel}`}
        </a>
      )}
    </>
  );
}
