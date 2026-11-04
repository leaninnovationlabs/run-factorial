import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv, searchForWorkspaceRoot } from "vite";
import { thirdPartyLicenses } from "./licenses";

function apiOrigin(raw: string | undefined): string {
  const fallback = "http://127.0.0.1:8000";
  try {
    return new URL(raw || fallback).origin;
  } catch {
    return fallback;
  }
}

// Under `make dev` the API is reached cross-origin, not through a dev proxy, so the dev server
// exercises the CORS + bearer-token path the API admits a browser by (FACTORIAL_CORS_ORIGINS).
// The one exception is the prefix's `/auth`, proxied so OAuth can return to the dashboard origin.
// The built bundle is served by the API itself, under the same prefix (decision 74).
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const api = apiOrigin(env.VITE_API_BASE_URL);
  return {
    base: "/factorial/",
    plugins: [react(), tailwindcss(), thirdPartyLicenses(process.cwd())],
    server: {
      port: 5173,
      // The design system's guide renders the doc itself (`pages/GuidePage.tsx`), and the doc
      // lives outside `ui/`, which is all the dev server serves by default.
      fs: { allow: [searchForWorkspaceRoot(process.cwd()), "../docs/design-system.md"] },
      proxy: {
        "/factorial/auth": { target: api, changeOrigin: true },
      },
    },
    build: {
      rollupOptions: {
        output: {
          // The libraries in a chunk of their own: every merge deploys (decision 47), and a deploy
          // that changes only the app then leaves the browser's copy of these valid.
          manualChunks: {
            vendor: [
              "react",
              "react-dom",
              "react-dom/client",
              "react-router-dom",
              "@tanstack/react-query",
            ],
          },
        },
      },
    },
    test: { environment: "node" },
  };
});
