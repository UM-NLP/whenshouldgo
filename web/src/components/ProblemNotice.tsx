import { useEffect, useState } from "react";

import { failureText } from "../api/failures";
import { geoErrorText } from "../lib/geo";
import type { PendingProblem } from "../useChat";

interface Props {
  problem: PendingProblem;
  sending: boolean;
  onRetry(): void;
  onUseLocation(): void;
}

function useCountdown(seconds: number | null): number {
  const [left, setLeft] = useState(seconds ?? 0);
  useEffect(() => {
    setLeft(seconds ?? 0);
    if (!seconds) return;
    const timer = setInterval(() => setLeft((value) => Math.max(0, value - 1)), 1000);
    return () => clearInterval(timer);
  }, [seconds]);
  return left;
}

export function ProblemNotice({ problem, sending, onRetry, onUseLocation }: Props) {
  const waitFor =
    problem.type === "api" && problem.failure.kind === "rate_limited"
      ? problem.failure.retryAfterSeconds
      : null;
  const left = useCountdown(waitFor);

  if (problem.type === "geo") {
    return (
      <div className="notice notice-error" role="alert">
        <p>{geoErrorText(problem.kind)}</p>
        <p>Your message has not been sent. Use &quot;Stop using my location&quot; to send without it.</p>
        <button type="button" onClick={onRetry} disabled={sending}>
          Try again
        </button>
      </div>
    );
  }

  const { failure, retry } = problem;
  return (
    <div className="notice notice-error" role="alert">
      <p>{failureText(failure)}</p>
      {failure.kind === "location_consent_required" ? (
        <div className="notice-actions">
          <button type="button" onClick={onUseLocation} disabled={sending}>
            Use my location
          </button>
          <button type="button" className="secondary" onClick={onRetry} disabled={sending}>
            Try without location
          </button>
        </div>
      ) : retry !== "none" ? (
        <button type="button" onClick={onRetry} disabled={sending || left > 0}>
          {left > 0 ? `Try again in ${left}s` : "Try again"}
        </button>
      ) : null}
    </div>
  );
}
