// First, so the bundle's stylesheet opens with it (see `index.css`).
import "./index.css";
import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App";
import { setOrganization } from "./api/client";
import { AuthProvider, expireSession } from "./auth";
import { basename, DEFAULT_ORG, enter, OrgProvider, rememberedOrganization } from "./org";
import { APP_PREFIX } from "./prefix";
import { createQueryClient } from "./queryClient";
import { ThemeProvider } from "./theme";

const queryClient = createQueryClient(expireSession);

// Before the router exists: its basename is the organization the URL names (`org.tsx`).
const entry = enter(window.location, rememberedOrganization());
if (entry.redirect) window.history.replaceState(window.history.state, "", entry.redirect);
if (entry.org) setOrganization(entry.org);

const container = document.getElementById("root");
if (!container) throw new Error("#root is missing from index.html");

createRoot(container).render(
  <StrictMode>
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <BrowserRouter basename={entry.org ? basename(entry.org) : APP_PREFIX}>
            <OrgProvider org={entry.org ?? DEFAULT_ORG}>
              <App />
            </OrgProvider>
          </BrowserRouter>
        </AuthProvider>
      </QueryClientProvider>
    </ThemeProvider>
  </StrictMode>,
);
