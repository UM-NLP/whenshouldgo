import { useEffect, useState } from "react";

import type { LocalStore } from "../storage";
import { guestRetentionText, historyFailureText, RETENTION_CHOICES, serverRetentionText } from "./copy";
import type { HistorySettings, Phase1Api } from "./historyApi";
import type { HistoryFailure, RepoMode } from "./types";

function Choice({ days, options, onPick }: { days: number; options: number[]; onPick(days: number): void }) {
  const all = options.includes(days) ? options : [...options, days].sort((a, b) => a - b);
  return (
    <label>
      Keep my history for{" "}
      <select value={String(days)} onChange={(e) => onPick(Number(e.target.value))}>
        {all.map((d) => (
          <option key={d} value={String(d)}>
            {d} days
          </option>
        ))}
      </select>
    </label>
  );
}

/** Registered users: the server's period (GET/PUT /v1/history/settings). Shortening purges older entries at once. */
function ServerRetention({ api, onChanged, onConsentLost }: { api: Phase1Api; onChanged(): void; onConsentLost?(): void }) {
  const [settings, setSettings] = useState<HistorySettings | null>(null);
  const [problem, setProblem] = useState<HistoryFailure | null>(null);
  const [saving, setSaving] = useState(false);
  const lost = (failure: HistoryFailure) => {
    if (failure.kind === "history_consent_required") onConsentLost?.();
    else setProblem(failure);
  };
  useEffect(() => {
    let live = true;
    void api.getSettings().then((o) => {
      if (!live) return;
      if (o.ok) setSettings(o.data);
      else lost(o.failure);
    });
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api]);

  if (!settings) {
    return problem ? (
      <p className="notice notice-error" role="alert">
        {historyFailureText(problem)}
      </p>
    ) : null;
  }
  const options = RETENTION_CHOICES.filter((d) => d >= settings.min_retention_days && d <= settings.max_retention_days);
  return (
    <div className="retention">
      <p className="muted" role="status">
        {serverRetentionText(settings.retention_days, settings.default_retention_days, settings.max_retention_days)}
      </p>
      <Choice
        days={settings.retention_days}
        options={options}
        onPick={(days) => {
          if (saving) return;
          setSaving(true);
          void api.putSettings(days).then((o) => {
            setSaving(false);
            if (o.ok) {
              setSettings(o.data);
              setProblem(null);
              onChanged();
            } else lost(o.failure);
          });
        }}
      />
      {problem ? (
        <p className="notice notice-error" role="alert">
          {historyFailureText(problem)}
        </p>
      ) : null}
    </div>
  );
}

/**
 * States the retention period and lets the user shorten it (30 to 180 days, default 90). Guests use the
 * local store's retention API; registered users use the server's settings. Either way, shortening
 * purges older entries at once.
 */
export function RetentionPicker({
  mode,
  store,
  api,
  onChanged,
  onConsentLost,
}: {
  mode: RepoMode;
  store: LocalStore | null;
  api?: Phase1Api;
  onChanged(): void;
  onConsentLost?(): void;
}) {
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

  if (mode === "server") return api ? <ServerRetention api={api} onChanged={onChanged} onConsentLost={onConsentLost} /> : null;
  if (!store) return null;
  if (problem) return <p className="notice notice-error" role="alert">Could not read your retention period.</p>;
  if (days === null) return null;

  return (
    <div className="retention">
      <p className="muted" role="status">
        {guestRetentionText(days)}
      </p>
      <Choice
        days={days}
        options={RETENTION_CHOICES}
        onPick={(next) => {
          void store.setRetentionDays(next).then(
            (applied) => {
              setDays(applied);
              setProblem(false);
              onChanged();
            },
            () => setProblem(true),
          );
        }}
      />
    </div>
  );
}
