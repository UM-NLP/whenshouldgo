// The Phase 1 slice of the backend contract: history records, saved places and consents. Everything
// here is reached only when the server's Capabilities.history_enabled is true.
import type { components } from "../api/schema";
import { classifyBoardFailure, type BoardFailure } from "../board/boardApi";
import { createCaller, type CallerOptions, type Result } from "../lib/typedCall";

type S = components["schemas"];
export type OutingRecord = S["OutingRecord"];
export type SavedPlace = S["SavedPlace"];
export type SavedPlaceUpsert = S["SavedPlaceUpsert"];
export type ConsentPurpose = S["ConsentPurpose"];
export type HistorySettings = S["HistorySettings"];
export type HistoryRecordEdit = S["HistoryRecordEdit"];
export type SavedPlacePrompt = S["SavedPlacePrompt"];
export type AskStateView = S["AskStateView"];
export type AskDecision = S["AskDecision"];

export type Phase1Outcome<T> = Result<T, BoardFailure>;

export interface Phase1Api {
  listHistory(): Promise<Phase1Outcome<OutingRecord[]>>;
  clearHistory(): Promise<Phase1Outcome<number>>;
  editRecord(id: string, edit: Omit<HistoryRecordEdit, "schema_version" | "clear_label"> & { clear_label?: boolean }): Promise<Phase1Outcome<OutingRecord>>;
  deleteRecord(id: string): Promise<Phase1Outcome<number>>;
  getSettings(): Promise<Phase1Outcome<HistorySettings>>;
  putSettings(retentionDays: number): Promise<Phase1Outcome<HistorySettings>>;
  /** The fixed near-home question for one message (registered users). The message is not kept. */
  askCheck(message: string): Promise<Phase1Outcome<SavedPlacePrompt>>;
  putAskState(kind: S["SavedPlaceKind"], decision: AskDecision): Promise<Phase1Outcome<AskStateView>>;
  listSaved(): Promise<Phase1Outcome<SavedPlace[]>>;
  putSaved(place: SavedPlaceUpsert): Promise<Phase1Outcome<SavedPlace>>;
  /** Deletes one saved place by its place ID. */
  deleteSaved(placeId: string): Promise<Phase1Outcome<number>>;
  hasConsent(purpose: ConsentPurpose): Promise<Phase1Outcome<boolean>>;
  grantConsent(purpose: ConsentPurpose, noticeVersion: string): Promise<Phase1Outcome<null>>;
  withdrawConsent(purpose: ConsentPurpose): Promise<Phase1Outcome<null>>;
}

export function createPhase1Api(options: CallerOptions): Phase1Api {
  const { client, call } = createCaller<BoardFailure>(
    options,
    classifyBoardFailure,
    { kind: "auth" },
    { kind: "network" },
    { kind: "unknown" },
  );
  return {
    listHistory: async () => {
      const o = await call((authorization) => client.GET("/v1/history/records", { params: { header: { authorization } } }));
      return o.ok ? { ok: true, data: o.data.records } : o;
    },
    clearHistory: async () => {
      const o = await call((authorization) => client.DELETE("/v1/history/records", { params: { header: { authorization } } }));
      return o.ok ? { ok: true, data: o.data.deleted } : o;
    },
    editRecord: (id, edit) =>
      call((authorization) =>
        client.PATCH("/v1/history/records/{id}", {
          params: { header: { authorization }, path: { id } },
          body: { schema_version: 1, clear_label: false, ...edit },
        }),
      ),
    deleteRecord: async (id) => {
      const o = await call((authorization) =>
        client.DELETE("/v1/history/records/{id}", { params: { header: { authorization }, path: { id } } }),
      );
      return o.ok ? { ok: true, data: o.data.deleted } : o;
    },
    getSettings: () => call((authorization) => client.GET("/v1/history/settings", { params: { header: { authorization } } })),
    putSettings: (retentionDays) =>
      call((authorization) =>
        client.PUT("/v1/history/settings", {
          params: { header: { authorization } },
          body: { schema_version: 1, retention_days: retentionDays },
        }),
      ),
    askCheck: (message) =>
      call((authorization) =>
        client.POST("/v1/saved-places/ask-check", {
          params: { header: { authorization } },
          body: { schema_version: 1, message },
        }),
      ),
    putAskState: (kind, decision) =>
      call((authorization) =>
        client.PUT("/v1/saved-places/ask-state", {
          params: { header: { authorization } },
          body: { schema_version: 1, label_kind: kind, decision },
        }),
      ),
    listSaved: async () => {
      const o = await call((authorization) => client.GET("/v1/saved-places", { params: { header: { authorization } } }));
      return o.ok ? { ok: true, data: o.data.places } : o;
    },
    putSaved: (place) =>
      call((authorization) => client.PUT("/v1/saved-places", { params: { header: { authorization } }, body: place })),
    deleteSaved: async (placeId) => {
      const o = await call((authorization) =>
        client.DELETE("/v1/saved-places", { params: { header: { authorization }, query: { place_id: placeId } } }),
      );
      return o.ok ? { ok: true, data: o.data.deleted } : o;
    },
    hasConsent: async (purpose) => {
      const o = await call((authorization) => client.GET("/v1/me/consents", { params: { header: { authorization } } }));
      return o.ok ? { ok: true, data: o.data.consents.some((c) => c.purpose === purpose) } : o;
    },
    grantConsent: async (purpose, noticeVersion) => {
      const o = await call((authorization) =>
        client.POST("/v1/me/consents", {
          params: { header: { authorization } },
          body: { purpose, notice_version: noticeVersion },
        }),
      );
      return o.ok ? { ok: true, data: null } : o;
    },
    withdrawConsent: async (purpose) => {
      const o = await call((authorization) =>
        client.DELETE("/v1/me/consents/{purpose}", { params: { header: { authorization }, path: { purpose } } }),
      );
      return o.ok ? { ok: true, data: null } : o;
    },
  };
}

/** Used when no Phase 1 API is wired. */
export const UNAVAILABLE_PHASE1_API: Phase1Api = (() => {
  const no = async (): Promise<{ ok: false; failure: BoardFailure }> => ({ ok: false, failure: { kind: "unavailable" } });
  return {
    listHistory: no,
    clearHistory: no,
    editRecord: no,
    deleteRecord: no,
    getSettings: no,
    putSettings: no,
    askCheck: no,
    putAskState: no,
    listSaved: no,
    putSaved: no,
    deleteSaved: no,
    hasConsent: no,
    grantConsent: no,
    withdrawConsent: no,
  };
})();
