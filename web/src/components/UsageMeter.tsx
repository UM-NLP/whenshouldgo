import { useState } from "react";

import type { UsageView } from "../api/client";
import { CREATE_ACCOUNT_LABEL } from "../account/copy";

/** The gate wording of docs/access-tiers.md; every number comes from the server's view. */
export function gateText(usage: UsageView): string {
  if (usage.period === "LIFETIME") {
    const trips = usage.gate_reason === "TRIPS_LIMIT";
    const noun = trips ? "trips" : "turns";
    const limit = trips ? usage.activated_trips_limit : usage.user_turns_limit;
    const next = trips ? usage.registered_trip_limit : usage.registered_turn_limit;
    const benefit = next == null ? "" : ` and receive ${next} ${noun} every 30 days`;
    return `You used your ${limit} guest ${noun}. Create a free account to continue${benefit}.`;
  }
  const reset = usage.resets_at
    ? ` Your allowance resets on ${new Date(usage.resets_at).toLocaleDateString()}.`
    : "";
  return `You have used this period's free allowance.${reset}`;
}

const NOTICE_TEXT =
  "Create a free account any time to recover your trips and keep them in sync across your devices.";

/**
 * Renders the server's meter_state. It computes no entitlement and stores nothing. `onCreateAccount`
 * is passed only for a guest whose server says it can link an account; without it no button shows.
 */
export function UsageMeter({ usage, onCreateAccount }: { usage: UsageView; onCreateAccount?: () => void }) {
  const [dismissedNotice, setDismissedNotice] = useState(false);
  const counters = `Turns ${usage.user_turns_used} of ${usage.user_turns_limit} · Trips ${usage.activated_trips_used} of ${usage.activated_trips_limit}`;
  switch (usage.meter_state) {
    case "QUIET_70":
      return (
        <p className="usage-meter" aria-label="Usage">
          {counters}
        </p>
      );
    case "NOTICE_90":
      return (
        <div className="usage-meter">
          <p aria-label="Usage">{counters}</p>
          {dismissedNotice ? null : (
            <div className="notice" role="status">
              <p>{NOTICE_TEXT}</p>
              <div className="notice-actions">
                {onCreateAccount ? (
                  <button type="button" onClick={onCreateAccount}>
                    {CREATE_ACCOUNT_LABEL}
                  </button>
                ) : null}
                <button type="button" className={onCreateAccount ? "secondary" : undefined} onClick={() => setDismissedNotice(true)}>
                  Dismiss
                </button>
              </div>
            </div>
          )}
        </div>
      );
    case "GATED":
      return (
        <div className="usage-meter notice notice-error" role="status">
          <p>{gateText(usage)}</p>
          <p>You can still read, export and delete your existing plans.</p>
          {onCreateAccount ? (
            <button type="button" onClick={onCreateAccount}>
              {CREATE_ACCOUNT_LABEL}
            </button>
          ) : null}
        </div>
      );
    default:
      return null;
  }
}
