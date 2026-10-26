import { useCallback, useEffect, useState } from "react";

import type { Api, UsageView } from "./api/client";

export interface Usage {
  /** The server's latest view, or null before bootstrap or when it failed (no meter is shown). */
  view: UsageView | null;
  update(view: UsageView): void;
}

/** Starts the session once on load and keeps the server's usage view in memory only. */
export function useUsage(api: Api): Usage {
  const [view, setView] = useState<UsageView | null>(null);
  useEffect(() => {
    let live = true;
    void api.bootstrap().then((outcome) => {
      if (live && outcome.ok) setView(outcome.data.usage);
    });
    return () => {
      live = false;
    };
  }, [api]);
  const update = useCallback((next: UsageView) => setView(next), []);
  return { view, update };
}
