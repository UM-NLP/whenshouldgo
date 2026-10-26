// Record shapes and the write guard for the on-device store (decisions 18, 19, 27).
// The guard is structural: only allow-listed fields of the right type can be written, so a
// provider payload, a title/address/rating or an exact coordinate has no field to land in.

export const SCHEMA_VERSION = 1;

export const USER_STORES = [
  "messages",
  "drafts",
  "plans",
  "reminders",
  "outing_log",
  "saved_places",
  "ask_state",
] as const;
export const ALL_STORES = [...USER_STORES, "outbox", "meta"] as const;
export type UserStoreName = (typeof USER_STORES)[number];
export type StoreName = (typeof ALL_STORES)[number];

export type OutingOutcome = "WENT" | "SKIPPED" | "CHANGED_PLAN" | "UNKNOWN";
export type SavedPlaceKind = "HOME" | "WORK" | "CUSTOM";

interface Versioned {
  schema_version: number;
}

/** The user's own text, or the server-authored reply text. Never the discovery block. */
export interface MessageRecord extends Versioned {
  id: string;
  seq: number;
  role: "user" | "assistant";
  text: string;
  /** User messages: the idempotency key of the send (matches an outbox item). */
  key?: string;
  /** Assistant turns: a flag only; the Google content itself is not stored. */
  had_discovery?: boolean;
  created_at: string;
}
export interface DraftRecord extends Versioned {
  id: string;
  text: string;
  updated_at: string;
}
/** An accepted plan: place ID, the user's label and a timestamp. No Maps content. */
export interface PlanRecord extends Versioned {
  id: string;
  place_id: string;
  label: string;
  accepted_at: string;
}
export interface ReminderRecord extends Versioned {
  id: string;
  place_id: string;
  label: string;
  due_at: string;
  created_at: string;
}
export interface OutingLogRecord extends Versioned {
  id: string;
  place_id: string;
  label: string;
  activity_tag: string;
  at: string;
  outcome: OutingOutcome;
}
/** Guest saved place. The position is user data, rounded to 3 decimals, never put in a key or URL. */
export interface SavedPlaceRecord extends Versioned {
  id: string;
  place_id: string;
  label: string;
  kind: SavedPlaceKind;
  position: { lat: number; lng: number } | null;
  updated_at: string;
}
export interface AskStateRecord extends Versioned {
  /** The user's label (or a label id), never a coordinate. */
  key: string;
  dont_ask_again: boolean;
  last_asked_at: string | null;
}
export interface MetaRecord extends Versioned {
  key: string;
  value: string | number | boolean;
}
export type OutboxState = "queued" | "stopped";
/** A queued turn. It never holds a location: `location_dropped` records that one was needed. */
export interface OutboxRecord extends Versioned {
  key: string;
  seq: number;
  text: string;
  created_at: string;
  state: OutboxState;
  stop_kind: string | null;
  location_dropped: boolean;
  not_before: number | null;
}

export interface RecordMap {
  messages: MessageRecord;
  drafts: DraftRecord;
  plans: PlanRecord;
  reminders: ReminderRecord;
  outing_log: OutingLogRecord;
  saved_places: SavedPlaceRecord;
  ask_state: AskStateRecord;
  outbox: OutboxRecord;
  meta: MetaRecord;
}

export const KEY_PATH: Record<StoreName, string> = {
  messages: "id",
  drafts: "id",
  plans: "id",
  reminders: "id",
  outing_log: "id",
  saved_places: "id",
  ask_state: "key",
  outbox: "key",
  meta: "key",
};

type Kind = "any" | "string" | "number" | "boolean" | "string?" | "number?" | "boolean?" | "object?";
const FIELDS: Record<StoreName, Record<string, Kind>> = {
  messages: { id: "string", seq: "number", role: "string", text: "string", key: "string?", had_discovery: "boolean?", created_at: "string" },
  drafts: { id: "string", text: "string", updated_at: "string" },
  plans: { id: "string", place_id: "string", label: "string", accepted_at: "string" },
  reminders: { id: "string", place_id: "string", label: "string", due_at: "string", created_at: "string" },
  outing_log: { id: "string", place_id: "string", label: "string", activity_tag: "string", at: "string", outcome: "string" },
  saved_places: { id: "string", place_id: "string", label: "string", kind: "string", position: "object?", updated_at: "string" },
  ask_state: { key: "string", dont_ask_again: "boolean", last_asked_at: "string?" },
  outbox: { key: "string", seq: "number", text: "string", created_at: "string", state: "string", stop_kind: "string?", location_dropped: "boolean", not_before: "number?" },
  meta: { key: "string", value: "any" },
};

export class StorageGuardError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "StorageGuardError";
  }
}

// A tripwire, not a classifier: a string that looks like provider content is refused.
const MAPS_CONTENT = /google\.com\/maps|maps\.app\.goo\.gl|goo\.gl\/maps|maps\.google\./i;

export function round3(value: number): number {
  return Math.round(value * 1000) / 1000;
}

function isRounded3(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && round3(value) === value;
}

/** Throws StorageGuardError unless `record` is exactly the allow-listed shape for `store`. */
export function assertWritable(store: StoreName, record: unknown): void {
  if (typeof record !== "object" || record === null) throw new StorageGuardError(`${store}: not an object`);
  const spec = FIELDS[store];
  const data = record as Record<string, unknown>;
  for (const name of Object.keys(data)) {
    if (name !== "schema_version" && !(name in spec)) throw new StorageGuardError(`${store}: field "${name}" is not allowed`);
  }
  if (data.schema_version !== SCHEMA_VERSION) throw new StorageGuardError(`${store}: schema_version`);
  for (const [name, kind] of Object.entries(spec)) {
    const value = data[name];
    const optional = kind.endsWith("?");
    const base = optional ? kind.slice(0, -1) : kind;
    if (value === undefined || value === null) {
      if (!optional) throw new StorageGuardError(`${store}: "${name}" is required`);
      continue;
    }
    if (base === "object") {
      const p = value as Record<string, unknown>;
      if (Object.keys(p).sort().join() !== "lat,lng" || !isRounded3(p.lat) || !isRounded3(p.lng)) {
        throw new StorageGuardError("saved_places: position must be lat/lng rounded to 3 decimals");
      }
      continue;
    }
    if (base !== "any" && typeof value !== base) throw new StorageGuardError(`${store}: "${name}" must be ${base}`);
    if (typeof value === "string" && MAPS_CONTENT.test(value)) {
      throw new StorageGuardError(`${store}: "${name}" looks like Maps content`);
    }
  }
  if (store === "messages" && data.role !== "user" && data.role !== "assistant") throw new StorageGuardError("messages: role");
  if (store === "saved_places" && !["HOME", "WORK", "CUSTOM"].includes(data.kind as string)) throw new StorageGuardError("saved_places: kind");
  if (store === "outbox" && data.state !== "queued" && data.state !== "stopped") throw new StorageGuardError("outbox: state");
  if (store === "meta" && !["string", "number", "boolean"].includes(typeof data.value)) throw new StorageGuardError("meta: value");
}
