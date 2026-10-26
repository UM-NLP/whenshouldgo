// Guest history and saved places on the device, through the guarded local store (decisions 18, 19,
// 27). The guard allows only place IDs, the user's labels, tags, times and a position rounded to 3
// decimals; it refuses Maps-looking strings. Reading and deleting need no notice; writing does.
import {
  NoticeRequiredError,
  SCHEMA_VERSION,
  round3,
  type LocalStore,
  type SavedPlaceKind,
  type SavedPlaceRecord,
} from "../storage";
import { outcomeText } from "./contractAdapter";
import type { HistoryFailure, HistoryOutcome, HistoryRepo, OutingEntry, Position, SavedEntry, SavedRepo } from "./types";

export const MAX_LABEL = 60;

function failure(error: unknown): { ok: false; failure: HistoryFailure } {
  return { ok: false, failure: error instanceof NoticeRequiredError ? { kind: "notice_required" } : { kind: "storage" } };
}

export function createDeviceHistoryRepo(store: LocalStore): HistoryRepo {
  return {
    mode: "device",
    canEditLabel: true,
    canDeleteOne: true,
    list: async () => {
      try {
        const rows = await store.getAll("outing_log");
        const data: OutingEntry[] = rows
          .map((r) => ({
            id: r.id,
            placeId: r.place_id,
            label: r.label || null,
            activityTag: r.activity_tag,
            at: r.at,
            outcomeText: outcomeText(r.outcome),
          }))
          .sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
        return { ok: true, data };
      } catch (error) {
        return failure(error);
      }
    },
    editLabel: async (id, label) => {
      try {
        const row = await store.get("outing_log", id);
        if (!row) return { ok: false, failure: { kind: "storage" } };
        await store.put("outing_log", { ...row, label: label.slice(0, MAX_LABEL) });
        return { ok: true, data: null };
      } catch (error) {
        return failure(error);
      }
    },
    deleteOne: async (id) => {
      try {
        await store.deleteItem("outing_log", id);
        return { ok: true, data: null };
      } catch (error) {
        return failure(error);
      }
    },
    clearAll: async () => {
      try {
        for (const row of await store.getAll("outing_log")) await store.deleteItem("outing_log", row.id);
        return { ok: true, data: null };
      } catch (error) {
        return failure(error);
      }
    },
    exportData: async () => {
      try {
        return {
          ok: true,
          data: {
            schema_version: SCHEMA_VERSION,
            exported_at: new Date().toISOString(),
            note: "Place IDs, your labels, activity tags and timestamps. Maps details are not stored.",
            outing_log: await store.getAll("outing_log"),
            saved_places: await store.getAll("saved_places"),
          },
        };
      } catch (error) {
        return failure(error);
      }
    },
  };
}

function toEntry(r: SavedPlaceRecord): SavedEntry | null {
  if (!r.position) return null;
  return { id: r.id, placeId: r.place_id, label: r.label, kind: r.kind, position: { latitude: r.position.lat, longitude: r.position.lng } };
}

function recordId(kind: SavedPlaceKind, placeId: string): string {
  // One Home and one Work; custom places are keyed by their place ID. No coordinate is ever in a key.
  return kind === "CUSTOM" ? `c-${placeId}` : kind.toLowerCase();
}

export function createDeviceSavedRepo(store: LocalStore): SavedRepo {
  return {
    mode: "device",
    list: async () => {
      try {
        const data = (await store.getAll("saved_places")).map(toEntry).filter((e): e is SavedEntry => e !== null);
        return { ok: true, data };
      } catch (error) {
        return failure(error);
      }
    },
    rename: async (entry, label) => {
      try {
        const row = await store.get("saved_places", entry.id);
        if (!row) return { ok: false, failure: { kind: "storage" } };
        await store.put("saved_places", { ...row, label: label.slice(0, MAX_LABEL), updated_at: new Date().toISOString() });
        return { ok: true, data: null };
      } catch (error) {
        return failure(error);
      }
    },
    remove: async (entry) => {
      try {
        await store.deleteItem("saved_places", entry.id);
        return { ok: true, data: null };
      } catch (error) {
        return failure(error);
      }
    },
    saveConfirmed: async ({ placeId, label, kind, position }) => {
      try {
        const pos: { lat: number; lng: number } = { lat: round3(position.latitude), lng: round3(position.longitude) };
        await store.put("saved_places", {
          schema_version: SCHEMA_VERSION,
          id: recordId(kind, placeId),
          place_id: placeId,
          label: label.slice(0, MAX_LABEL),
          kind,
          position: pos,
          updated_at: new Date().toISOString(),
        });
        return { ok: true, data: null };
      } catch (error) {
        return failure(error);
      }
    },
  };
}

export type { HistoryOutcome, Position };
