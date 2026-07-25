"use client";

/**
 * Client provider composition for the app shell.
 *
 * AuthProvider owns the session; TokenBridge feeds its token into
 * AppStateProvider's `authToken` prop (which existed unwired since the
 * provider was introduced — every API call was anonymous).
 */

import React from "react";

import { AppStateProvider } from "@/lib/app-state";
import { AuthProvider, useAuth } from "@/lib/auth";

function TokenBridge({ children }: { children: React.ReactNode }) {
  const { token, refreshSession } = useAuth();
  return (
    <AppStateProvider authToken={token} getValidToken={refreshSession}>
      {children}
    </AppStateProvider>
  );
}

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <AuthProvider>
      <TokenBridge>{children}</TokenBridge>
    </AuthProvider>
  );
}
