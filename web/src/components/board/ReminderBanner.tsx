import { useCallback, useEffect, useState } from "react";

import { classifyReminders, dismissReminder, loadReminders, snoozeReminder } from "../../board/reminders";
import { useNow } from "../../board/useNow";
import type { LocalStore, ReminderRecord } from "../../storage";
import type { FollowUp } from "../../useBoard";

function at(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

interface Props {
  store: LocalStore | null;
  /** Bumped when a reminder is saved so the banner re-reads the store. */
  token: number;
  onCheckIn(followUp: FollowUp): void;
}

/** Due and upcoming local reminders, read when the app opens (a closed tab cannot fire one). */
export function ReminderBanner({ store, token, onCheckIn }: Props) {
  const [reminders, setReminders] = useState<ReminderRecord[]>([]);
  const now = useNow(30_000);

  const refresh = useCallback(async () => {
    if (store) setReminders(await loadReminders(store));
  }, [store]);
  useEffect(() => {
    void refresh();
  }, [refresh, token]);

  if (!store) return null;
  const { due, upcoming } = classifyReminders(reminders, now);
  if (due.length === 0 && upcoming.length === 0) return null;

  async function act(run: () => Promise<void>) {
    try {
      await run();
    } catch {
      // The banner simply stays; nothing is surfaced from a storage error.
    }
    await refresh();
  }

  return (
    <section className="notice reminder-banner" role="status" aria-label="Reminders">
      {due.map((r) => (
        <div key={r.id} className="reminder">
          <p>{`Time to head to "${r.label}".`}</p>
          <div className="notice-actions">
            <button
              type="button"
              onClick={() => {
                onCheckIn({ placeId: r.place_id, label: r.label });
                void act(() => dismissReminder(store, r.id));
              }}
            >
              Did you go?
            </button>
            <button type="button" className="secondary" onClick={() => void act(() => snoozeReminder(store, r, Date.now()))}>
              Snooze 10 minutes
            </button>
            <button type="button" className="secondary" onClick={() => void act(() => dismissReminder(store, r.id))}>
              Dismiss
            </button>
          </div>
        </div>
      ))}
      {upcoming.map((r) => (
        <div key={r.id} className="reminder">
          <p>{`Coming up at ${at(r.due_at)}: "${r.label}".`}</p>
          <button type="button" className="secondary" onClick={() => void act(() => dismissReminder(store, r.id))}>
            Dismiss
          </button>
        </div>
      ))}
    </section>
  );
}
