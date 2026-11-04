/**
 * The one line the whole app says when it cannot do its job: a newer version was deployed under
 * this tab, the browser is offline, or the API has stopped answering. Said once, at the top, so a
 * page does not fill with an error on every panel that polls — each keeps what it last read.
 */

import { type QueryClient, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { ApiError } from "../../api/client";
import { Banner } from "../ui";

/** Failed reads in a row, with none succeeding between them, before the API counts as gone. */
export const UNREACHABLE_AFTER = 3;

/** A failure that says the API is not answering, as against one that answered no: a 404 or a 409
 *  is the API working. `request` reports an unreachable API as status 0. */
export function outage(error: unknown): boolean {
  return !(error instanceof ApiError) || error.status === 0 || error.status >= 500;
}

function useApiUnreachable(client: QueryClient): boolean {
  const [failures, setFailures] = useState(0);
  useEffect(
    () =>
      client.getQueryCache().subscribe((event) => {
        if (event.type !== "updated") return;
        if (event.action.type === "success") setFailures(0);
        else if (event.action.type === "error" && outage(event.action.error)) {
          setFailures((count) => count + 1);
        }
      }),
    [client],
  );
  return failures >= UNREACHABLE_AFTER;
}

function useOnline(): boolean {
  const [online, setOnline] = useState(() =>
    typeof navigator === "undefined" ? true : navigator.onLine,
  );
  useEffect(() => {
    const up = () => setOnline(true);
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, []);
  return online;
}

/** Vite's signal that a chunk this tab asked for is no longer on the server — every merge
 *  deploys (decision 47), and the old files go with the old build. */
function useNewVersion(): boolean {
  const [stale, setStale] = useState(false);
  useEffect(() => {
    // Not `preventDefault()`: that tells Vite the failure was handled, and the import then
    // resolves to `undefined` and the page crashes on it. Left alone it rejects, and the route's
    // error boundary says the page failed while this banner offers the reload.
    const onPreloadError = () => setStale(true);
    window.addEventListener("vite:preloadError", onPreloadError);
    return () => window.removeEventListener("vite:preloadError", onPreloadError);
  }, []);
  return stale;
}

export function ConnectionBanner() {
  const client = useQueryClient();
  const unreachable = useApiUnreachable(client);
  const online = useOnline();
  const stale = useNewVersion();

  if (stale) {
    return (
      <Banner
        appearance="announcement"
        action={{ text: "Reload", onClick: () => window.location.reload() }}
      >
        A new version of Factorial is available.
      </Banner>
    );
  }
  if (!online) {
    return (
      <Banner appearance="warning">
        You're offline. Pages show what they last loaded and update when you reconnect.
      </Banner>
    );
  }
  if (unreachable) {
    return (
      <Banner appearance="error">
        Factorial isn't responding, so this page may be out of date. Reconnecting.
      </Banner>
    );
  }
  return null;
}
