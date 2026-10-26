import { useCallback, useEffect, useState } from "react";

import type { Api, SessionBootstrap, UsageView } from "./api/client";

export interface Usage {
  /** The server's latest view, or null before bootstrap or when it failed (no meter is shown). */
  view: UsageView | null;
  /** What the server says this session may do, and its tier. Never derived on the client. */
  capabilities: SessionBootstrap["capabilities"] | null;
  tier: SessionBootstrap["tier"] | null;
  update(view: UsageView): void;
  /** Asks the server again (for example after the account was linked). */
  refresh(): void;
}

/** Starts the session once on load and keeps the server's usage view in memory only. */
export function useUsage(api: Api): Usage {
  const [view, setView] = useState<UsageView | null>(null);
  const [session, setSession] = useState<SessionBootstrap | null>(null);
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    let live = true;
    void api.bootstrap().then((outcome) => {
      if (live && outcome.ok) {
        setView(outcome.data.usage);
        setSession(outcome.data);
      }
    });
    return () => {
      live = false;
    };
  }, [api, nonce]);
  const update = useCallback((next: UsageView) => setView(next), []);
  const refresh = useCallback(() => setNonce((n) => n + 1), []);
  return { view, capabilities: session?.capabilities ?? null, tier: session?.tier ?? null, update, refresh };
}
