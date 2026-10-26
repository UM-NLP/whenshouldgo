import { useEffect, useState } from "react";

import type { LocalStore } from "../storage";
import { guestRetentionText, RETENTION_CHOICES, SERVER_RETENTION_TEXT } from "./copy";
import { SERVER_RETENTION_PICKER } from "./contractAdapter";
import type { RepoMode } from "./types";

/**
 * States the retention period. Guests can shorten it (30 to 180 days, default 90) through the local
 * store's retention API, which purges older entries at once. Registered users see the server's
 * period (the contract has no shorter-retention setting yet; see contractAdapter.ts).
 */
export function RetentionPicker({ mode, store, onChanged }: { mode: RepoMode; store: LocalStore | null; onChanged(): void }) {
  const [days, setDays] = useState<number | null>(null);
  const [problem, setProblem] = useState(false);
  useEffect(() => {
    if (mode !== "device" || !store) return;
    let live = true;
    void store.retentionDays().then(
      (d) => live && setDays(d),
      () => live && setProblem(true),
    );
    return () => {
      live = false;
    };
  }, [mode, store]);

  if (mode === "server") return <p className="muted">{SERVER_RETENTION_PICKER ? null : SERVER_RETENTION_TEXT}</p>;
  if (!store) return null;
  if (problem) return <p className="notice notice-error" role="alert">Could not read your retention period.</p>;
  if (days === null) return null;

  const options = RETENTION_CHOICES.includes(days) ? RETENTION_CHOICES : [...RETENTION_CHOICES, days].sort((a, b) => a - b);
  return (
    <div className="retention">
      <p className="muted" role="status">
        {guestRetentionText(days)}
      </p>
      <label>
        Keep my history for{" "}
        <select
          value={String(days)}
          onChange={(e) => {
            const next = Number(e.target.value);
            void store.setRetentionDays(next).then(
              (applied) => {
                setDays(applied);
                setProblem(false);
                onChanged();
              },
              () => setProblem(true),
            );
          }}
        >
          {options.map((d) => (
            <option key={d} value={String(d)}>
              {d} days
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}
