// THE ONE FILE THAT DEPENDS ON BACKEND CONTRACT DETAILS THAT ARE STILL MOVING (Phase 1, decisions 24-25).
//
// Everything the UI needs from the server's history, saved-place and ask-state contracts goes through
// here, so reconciling with the backend's final contract is an edit to this file. Known gaps in the
// schema as of this writing (each marked RECONCILE):
//
//   1. SAVE_PLACE propose carries no position. The client keeps the rounded position in memory and,
//      after the server CONFIRMS the action, finishes the save (PUT /v1/saved-places). When the
//      backend adds the position to the propose request, set SERVER_SAVE_PLACE_CARRIES_POSITION and
//      add the field in `proposalExtras` / boardApi.propose.
//   2. There is no ask-state or "near home" signal in any response. `askSignalFromTurn` returns null,
//      so registered users are never asked until the server provides one. (Guests use the local
//      deterministic rule in places/SavedAskPolicy.ts.)
//   3. There is no endpoint to edit or delete ONE history record, or to set a shorter server
//      retention. The server repo reports those as unavailable.
//   4. A position-only Home has no Google place ID. `positionTargetId()` makes an opaque ID that matches
//      the server's place-ID pattern; replace it when the backend defines the target for such a save.
//   5. Consent notice versions below are placeholders; the server must approve the real ones (#27).
import type { TurnResponse } from "../api/client";
import type { SavedPlaceKind } from "../storage";
import type { ConsentPurpose, OutingRecord, Phase1Api, SavedPlace } from "./historyApi";
import type { HistoryOutcome, HistoryRepo, OutingEntry, SavedEntry, SavedRepo } from "./types";

export const HISTORY_NOTICE_VERSION = "history-notice-1";
export const SAVED_PLACE_NOTICE_VERSION = "saved-place-notice-1";
export const NOTICE_VERSIONS: Partial<Record<ConsentPurpose, string>> = {
  HISTORY_STORE: HISTORY_NOTICE_VERSION,
  SAVED_PLACE: SAVED_PLACE_NOTICE_VERSION,
};

/** RECONCILE 3. */
export const SERVER_HISTORY_CAN_EDIT_ONE = false;
export const SERVER_HISTORY_CAN_DELETE_ONE = false;
export const SERVER_RETENTION_PICKER = false;
/** The server's rolling ceiling in days (decision 19); shown as text only. */
export const SERVER_RETENTION_CEILING_DAYS = 180;

/** RECONCILE 1. */
export const SERVER_SAVE_PLACE_CARRIES_POSITION = false;

export type AskKind = "HOME" | "WORK";

/** RECONCILE 2: a typed field on the turn (or a capability) will say "ask the Home question now". */
export function askSignalFromTurn(_turn: TurnResponse): AskKind | null {
  return null;
}

/** RECONCILE 4. */
export function positionTargetId(): string {
  const bytes = new Uint8Array(9);
  globalThis.crypto.getRandomValues(bytes);
  return `here-${Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")}`;
}

const OUTCOME_TEXT: Record<string, string> = {
  WENT: "Went",
  SKIPPED: "Did not go",
  CHANGED_PLAN: "Changed plan",
};
export function outcomeText(outcome: string): string | null {
  return OUTCOME_TEXT[outcome] ?? null;
}

function serverOuting(record: OutingRecord): OutingEntry {
  return {
    id: record.id,
    placeId: record.place_ref.place_id,
    label: record.user_label ?? null,
    activityTag: record.intent_tag,
    at: record.occurred_at,
    outcomeText: record.strength === "SELF_REPORTED_VISIT" ? "Went (you confirmed)" : "Opened in Maps",
  };
}

function serverSaved(place: SavedPlace): SavedEntry {
  return {
    id: place.place_id,
    placeId: place.place_id,
    label: place.user_label,
    kind: place.label_kind,
    position: { latitude: place.position.latitude, longitude: place.position.longitude },
  };
}

const unavailable = { ok: false, failure: { kind: "unavailable" } } as const;

/** History on the server, for a registered user. */
export function createServerHistoryRepo(api: Phase1Api, exportAccount: () => Promise<HistoryOutcome<unknown>>): HistoryRepo {
  return {
    mode: "server",
    canEditLabel: SERVER_HISTORY_CAN_EDIT_ONE,
    canDeleteOne: SERVER_HISTORY_CAN_DELETE_ONE,
    list: async () => {
      const o = await api.listHistory();
      if (!o.ok) return o;
      return { ok: true, data: o.data.map(serverOuting).sort((a, b) => Date.parse(b.at) - Date.parse(a.at)) };
    },
    editLabel: async () => unavailable,
    deleteOne: async () => unavailable,
    clearAll: async () => {
      const o = await api.clearHistory();
      return o.ok ? { ok: true, data: null } : o;
    },
    exportData: async () => {
      const o = await exportAccount();
      if (!o.ok) return o;
      // Only the history slice of the account export: outings and saved places.
      const bundle = o.data as { outings?: unknown; saved_places?: unknown; generated_at?: unknown };
      return { ok: true, data: { generated_at: bundle.generated_at, outings: bundle.outings ?? [], saved_places: bundle.saved_places ?? [] } };
    },
  };
}

/** Saved places on the server, for a registered user. */
export function createServerSavedRepo(api: Phase1Api): SavedRepo {
  return {
    mode: "server",
    list: async () => {
      const o = await api.listSaved();
      return o.ok ? { ok: true, data: o.data.map(serverSaved) } : o;
    },
    // Renaming edits an existing saved place (same ID, kind and position); it creates nothing.
    rename: async (entry, label) => {
      const o = await api.putSaved({
        schema_version: 1,
        place_id: entry.placeId,
        label_kind: entry.kind,
        user_label: label,
        position: entry.position,
      });
      return o.ok ? { ok: true, data: null } : o;
    },
    remove: async (entry) => {
      const o = await api.deleteSaved(entry.placeId);
      return o.ok ? { ok: true, data: null } : o;
    },
    saveConfirmed: async (input: { placeId: string; label: string; kind: SavedPlaceKind; position: { latitude: number; longitude: number } }) => {
      if (SERVER_SAVE_PLACE_CARRIES_POSITION) return { ok: true, data: null }; // the server already wrote it at confirm
      const o = await api.putSaved({
        schema_version: 1,
        place_id: input.placeId,
        label_kind: input.kind,
        user_label: input.label,
        position: input.position,
      });
      return o.ok ? { ok: true, data: null } : o;
    },
  };
}
