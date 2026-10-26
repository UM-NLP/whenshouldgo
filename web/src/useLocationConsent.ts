import { useCallback, useEffect, useRef, useState } from "react";

import type { Api } from "./api/client";
import { failureText } from "./api/failures";
import { GeoError, geoErrorText, getForegroundPosition, type Coordinates } from "./lib/geo";

/**
 * "unknown": not asked yet. "off": no consent. "notice": showing the placeholder notice.
 * "working": consent call or permission check in flight. "on": consent is active, so each send
 * reads the position inside the tap that sends it.
 */
export type LocationPhase = "unknown" | "off" | "notice" | "working" | "on";

export interface LocationConsent {
  phase: LocationPhase;
  /** Plain-language problem from the last attempt, or null. */
  problem: string | null;
  /** Step 1: show the notice. */
  start(): void;
  cancel(): void;
  /** Step 2: the user agreed. POST the consent, then ask the browser. Resolves true on success. */
  accept(): Promise<boolean>;
  stop(): Promise<void>;
  /** Called from a user's tap (Send). Never watches; the result is not kept. */
  readPosition(): Promise<Coordinates>;
  /** The server no longer holds our consent (for example after a restart). */
  markOff(): void;
}

export function useLocationConsent(api: Api): LocationConsent {
  const [phase, setPhase] = useState<LocationPhase>("unknown");
  const [problem, setProblem] = useState<string | null>(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    void api.hasLocationConsent().then((outcome) => {
      if (alive.current) setPhase(outcome.ok && outcome.data ? "on" : "off");
    });
    return () => {
      alive.current = false;
    };
  }, [api]);

  const start = useCallback(() => {
    setProblem(null);
    setPhase("notice");
  }, []);

  const cancel = useCallback(() => setPhase("off"), []);

  const accept = useCallback(async () => {
    setPhase("working");
    setProblem(null);
    const granted = await api.grantLocationConsent();
    if (!granted.ok) {
      setProblem(failureText(granted.failure));
      setPhase("off");
      return false;
    }
    try {
      await getForegroundPosition(); // permission check only; the coordinates are dropped here
    } catch (error) {
      // No usable location, so do not leave a consent standing that nothing can use.
      await api.withdrawLocationConsent();
      setProblem(geoErrorText(error instanceof GeoError ? error.kind : "unavailable"));
      setPhase("off");
      return false;
    }
    setPhase("on");
    return true;
  }, [api]);

  const stop = useCallback(async () => {
    setProblem(null);
    const outcome = await api.withdrawLocationConsent();
    // A 404 means there was nothing to withdraw, which is the state the user asked for.
    if (!outcome.ok && outcome.failure.kind !== "unknown") {
      setProblem(failureText(outcome.failure));
      return;
    }
    setPhase("off");
  }, [api]);

  const markOff = useCallback(() => setPhase("off"), []);

  return { phase, problem, start, cancel, accept, stop, readPosition: getForegroundPosition, markOff };
}
