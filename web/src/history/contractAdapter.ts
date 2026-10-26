// Where the web client meets the backend's Phase 1 contract (history, saved places, ask-state, notices).
// Reconciled with the final backend contract (2026-10-26); what is still a client-side assumption is
// listed here so it is easy to find:
//
//   - Notice versions. The server accepts exactly ONE notice version for both history purposes: the
//     `history_notice_version` in its runtime config, which must also be on the owner-approved list
//     (domain.APPROVED_HISTORY_NOTICE_VERSIONS, empty until counsel signs off, issue #27). The server
//     does not publish it in any response, so the string below is a PLACEHOLDER that must be changed
//     in step with that config. Both purposes send the same string.
//   - A position-only save has no Google place ID. The contract wants a `PlaceId`-shaped key
//     (`[A-Za-z0-9_-]{1,300}`) and the server stores one Home and one Work per user whatever the key
//     is, so Home and Work use a fixed per-kind key (`saveTargetId`). A custom place needs the place
//     ID of a place the user picked; there is no other legitimate ID.
import type { TurnResponse } from "../api/client";
import type { SavedPlaceKind } from "../storage";
import type { ConsentPurpose, OutingRecord, Phase1Api, SavedPlace } from "./historyApi";
import type { HistoryOutcome, HistoryRepo, OutingEntry, SavedEntry, SavedRepo } from "./types";

/** PLACEHOLDER pending counsel (issue #27). Must equal the server's configured history_notice_version. */
export const HISTORY_NOTICE_VERSION = "history-notice-1";
/** The server uses one notice version for both history purposes, so this is the same string. */
export const SAVED_PLACE_NOTICE_VERSION = HISTORY_NOTICE_VERSION;
export const NOTICE_VERSIONS: Partial<Record<ConsentPurpose, string>> = {
  HISTORY_STORE: HISTORY_NOTICE_VERSION,
  SAVED_PLACE: SAVED_PLACE_NOTICE_VERSION,
};

/** The server's rolling ceiling in days (decision 19); the server also reports it in GET /v1/history/settings. */
export const SERVER_RETENTION_CEILING_DAYS = 180;

export type AskKind = "HOME" | "WORK";

/** The server's near-home question for this turn, if it asked one. The text is server-authored. */
export interface AskSignal {
  kind: AskKind;
  question: string | null;
}

export function askSignalFromTurn(turn: TurnResponse): AskSignal | null {
  const prompt = turn.saved_place_prompt;
  if (!prompt || !prompt.ask) return null;
  if (prompt.label_kind !== "HOME" && prompt.label_kind !== "WORK") return null;
  return { kind: prompt.label_kind, question: prompt.question ?? null };
}

/** The `target` of a SAVE_PLACE action. See the header: a fixed per-kind key for a position-only save. */
export function saveTargetId(kind: SavedPlaceKind, selectedPlaceId?: string | null): string | null {
  if (selectedPlaceId) return selectedPlaceId;
  if (kind === "HOME") return "saved-home";
  if (kind === "WORK") return "saved-work";
  return null; // CUSTOM needs the ID of a place the user picked
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

/** History on the server, for a registered user. */
export function createServerHistoryRepo(api: Phase1Api, exportAccount: () => Promise<HistoryOutcome<unknown>>): HistoryRepo {
  return {
    mode: "server",
    canEditLabel: true,
    canDeleteOne: true,
    list: async () => {
      const o = await api.listHistory();
      if (!o.ok) return o;
      return { ok: true, data: o.data.map(serverOuting).sort((a, b) => Date.parse(b.at) - Date.parse(a.at)) };
    },
    editLabel: async (id, label) => {
      const text = label.trim();
      const o = await api.editRecord(id, text ? { user_label: text } : { clear_label: true });
      return o.ok ? { ok: true, data: null } : o;
    },
    deleteOne: async (id) => {
      const o = await api.deleteRecord(id);
      return o.ok ? { ok: true, data: null } : o;
    },
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
  };
}
