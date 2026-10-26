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

export type Phase1Outcome<T> = Result<T, BoardFailure>;

export interface Phase1Api {
  listHistory(): Promise<Phase1Outcome<OutingRecord[]>>;
  clearHistory(): Promise<Phase1Outcome<number>>;
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
    listSaved: no,
    putSaved: no,
    deleteSaved: no,
    hasConsent: no,
    grantConsent: no,
    withdrawConsent: no,
  };
})();
