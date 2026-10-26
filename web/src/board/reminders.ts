// Local reminders and the local outing log, on top of the existing storage layer (which owns the
// allow-listed fields and the first-save notice). Only a place ID, the user's own label and times
// are written: no Maps content, no coordinates.
import { SCHEMA_VERSION, type LocalStore, type OutcomeValueLocal, type ReminderRecord } from "./storageTypes";
import { localOutcome } from "./viewModels";
import type { OutcomeValue } from "./boardApi";

export const UPCOMING_WINDOW_MS = 60 * 60 * 1000;
export const SNOOZE_MS = 10 * 60 * 1000;

function id(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

export async function loadReminders(store: LocalStore): Promise<ReminderRecord[]> {
  try {
    return (await store.getAll("reminders")).sort((a, b) => Date.parse(a.due_at) - Date.parse(b.due_at));
  } catch {
    return [];
  }
}

export function classifyReminders(reminders: ReminderRecord[], now: number) {
  const due: ReminderRecord[] = [];
  const upcoming: ReminderRecord[] = [];
  for (const r of reminders) {
    const at = Date.parse(r.due_at);
    if (Number.isNaN(at)) continue;
    if (at <= now) due.push(r);
    else if (at - now <= UPCOMING_WINDOW_MS) upcoming.push(r);
  }
  return { due, upcoming };
}

export async function saveReminder(
  store: LocalStore,
  input: { placeId: string; label: string; dueAt: Date },
): Promise<void> {
  await store.put("reminders", {
    schema_version: SCHEMA_VERSION,
    id: id("r"),
    place_id: input.placeId,
    label: input.label,
    due_at: input.dueAt.toISOString(),
    created_at: new Date().toISOString(),
  });
}

export async function snoozeReminder(store: LocalStore, reminder: ReminderRecord, now: number): Promise<void> {
  await store.put("reminders", { ...reminder, due_at: new Date(now + SNOOZE_MS).toISOString() });
}

export async function dismissReminder(store: LocalStore, reminderId: string): Promise<void> {
  await store.deleteItem("reminders", reminderId);
}

/** The guest's own outing log: place ID, the user's label and the outcome. No note, no Maps content. */
export async function logOutcomeLocally(
  store: LocalStore,
  input: { placeId: string; label: string; outcome: OutcomeValue },
): Promise<void> {
  const outcome: OutcomeValueLocal = localOutcome(input.outcome);
  await store.put("outing_log", {
    schema_version: SCHEMA_VERSION,
    id: id("o"),
    place_id: input.placeId,
    label: input.label,
    activity_tag: "OTHER",
    at: new Date().toISOString(),
    outcome,
  });
}
