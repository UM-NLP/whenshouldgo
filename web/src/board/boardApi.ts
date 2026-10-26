// The Trip Board's slice of the backend contract. Types come from the generated schema; the
// failure model is the board's own because "not available yet" (501, 503, DISABLED) and a 409
// (conflict) must stay distinguishable from a provider error. Server message text is never kept.
import createClient from "openapi-fetch";

import { AuthUnavailable, type AuthProvider } from "../auth";
import type { components, paths } from "../api/schema";
import { classifyFailure, type ErrorBody } from "../api/failures";
import type { Coordinates } from "../lib/geo";

type S = components["schemas"];
export type Workspace = S["WorkspaceView"];
export type Fact = S["Fact"];
export type FactEdit = S["FactEdit"];
export type PendingAction = S["PendingAction"];
export type PendingActionEffect = S["PendingActionEffect"];
export type PlaceCard = S["PlaceCard"];
export type RouteCheck = S["RouteCheck"];
export type OutcomeValue = S["OutcomeValue"];
export type IntentTag = S["IntentTag"];
export type OutcomeAck = S["OutcomeAck"];
export type ConfirmActionResponse = S["ConfirmActionResponse"];
export type ReminderItem = S["ReminderItem"];
export type TripActivation = S["TripActivation"];

export type BoardFailure =
  | { kind: "unavailable" }
  | { kind: "conflict" }
  | { kind: "consent_required" }
  | { kind: "guest_not_allowed" }
  | { kind: "history_consent_required" }
  | { kind: "quota" }
  | { kind: "rate_limited"; retryAfterSeconds: number | null }
  | { kind: "auth" }
  | { kind: "provider" }
  | { kind: "network" }
  | { kind: "invalid" }
  | { kind: "unknown" };

export type BoardOutcome<T> = { ok: true; data: T } | { ok: false; failure: BoardFailure };

export function classifyBoardFailure(
  status: number,
  body: Partial<ErrorBody> | undefined,
  retryAfter: string | null,
): BoardFailure {
  const reason = body?.reason ?? undefined;
  if (reason === "LOCATION_CONSENT_REQUIRED") return { kind: "consent_required" };
  if (status === 409) return { kind: "conflict" };
  const base = classifyFailure(status, body, retryAfter);
  switch (base.kind) {
    case "guest_not_allowed":
    case "history_consent_required":
      return { kind: base.kind };
    case "unavailable":
      return { kind: "unavailable" };
    case "rate_limited":
      return base;
    case "quota":
    case "over_budget":
      return { kind: "quota" };
    case "auth":
      return { kind: "auth" };
    case "invalid":
      return { kind: "invalid" };
    case "network":
      return { kind: "network" };
    case "not_configured": // a service the server has not switched on (for example Places): not yet
      return { kind: "unavailable" };
    case "provider":
    case "discovery_unavailable":
    case "busy":
      return { kind: "provider" };
    default:
      return { kind: "unknown" };
  }
}

export interface EditInput {
  baseVersion: number;
  upsert: FactEdit[];
  remove: string[];
  idempotencyKey: string;
}
export interface ProposeInput {
  effect: PendingActionEffect;
  /** The place ID (never a name or address). */
  target: string;
  userLabel: string;
  workspaceId: string;
  planVersion: number;
  /** One key per proposal; a retry of the same proposal repeats it. */
  idempotencyKey: string;
  /** SET_REMINDER only: ISO time, within a day of now. The server's copy is the one that counts. */
  dueAt?: string;
  /** SAVE_PLACE only: HOME, WORK or CUSTOM (the server requires it for that effect and refuses it for others). */
  labelKind?: S["SavedPlaceKind"];
  /** SAVE_PLACE only (required by the server): the consented foreground position, rounded to 3 decimals. */
  position?: { latitude: number; longitude: number };
}
export interface OutcomeInput {
  placeId: string;
  outcome: OutcomeValue;
  note: string | null;
  idempotencyKey: string;
  /** The user's own label for the place, when they gave one. Never a Maps name. */
  userLabel?: string | null;
  /** The board's activity tag, when the board has one (the server derives one from the goal otherwise). */
  intentTag?: IntentTag | null;
}

export interface BoardApi {
  getWorkspace(): Promise<BoardOutcome<Workspace>>;
  editWorkspace(input: EditInput): Promise<BoardOutcome<Workspace>>;
  placeDetails(placeId: string): Promise<BoardOutcome<PlaceCard | null>>;
  routeCheck(placeId: string, location: Coordinates | null): Promise<BoardOutcome<RouteCheck>>;
  propose(input: ProposeInput): Promise<BoardOutcome<PendingAction>>;
  confirm(action: PendingAction): Promise<BoardOutcome<ConfirmActionResponse>>;
  cancel(actionId: string): Promise<BoardOutcome<null>>;
  recordOutcome(input: OutcomeInput): Promise<BoardOutcome<OutcomeAck>>;
  activateTrip(workspaceId: string, planVersion: number): Promise<BoardOutcome<TripActivation>>;
}

export interface BoardApiOptions {
  auth: AuthProvider;
  fetchImpl?: typeof fetch;
  baseUrl?: string;
}

interface Raw<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

export function createBoardApi({ auth, fetchImpl, baseUrl }: BoardApiOptions): BoardApi {
  const client = createClient<paths>({
    baseUrl: baseUrl ?? globalThis.location.origin,
    fetch: (request: Request) => (fetchImpl ?? globalThis.fetch)(request),
    credentials: "omit",
  });

  async function call<T>(run: (authorization: string) => Promise<Raw<T>>): Promise<BoardOutcome<T>> {
    let header: string;
    try {
      header = `Bearer ${await auth.getToken()}`;
    } catch (error) {
      return { ok: false, failure: error instanceof AuthUnavailable ? { kind: "auth" } : { kind: "unknown" } };
    }
    try {
      const { data, error, response } = await run(header);
      if (response.ok) {
        return data === undefined ? { ok: false, failure: { kind: "unknown" } } : { ok: true, data };
      }
      return {
        ok: false,
        failure: classifyBoardFailure(
          response.status,
          typeof error === "object" && error !== null ? (error as Partial<ErrorBody>) : undefined,
          response.headers.get("retry-after"),
        ),
      };
    } catch {
      return { ok: false, failure: { kind: "network" } };
    }
  }

  return {
    getWorkspace: () => call((authorization) => client.GET("/v1/workspace", { params: { header: { authorization } } })),
    editWorkspace: ({ baseVersion, upsert, remove, idempotencyKey }) =>
      call((authorization) =>
        client.PATCH("/v1/workspace", {
          params: { header: { authorization, "idempotency-key": idempotencyKey } },
          body: { schema_version: 1, base_version: baseVersion, upsert_facts: upsert, remove_fact_ids: remove },
        }),
      ),
    placeDetails: async (placeId) => {
      const outcome = await call((authorization) =>
        client.POST("/v1/places/details", {
          params: { header: { authorization } },
          body: { schema_version: 1, place_ids: [placeId] },
        }),
      );
      if (!outcome.ok) return outcome;
      return { ok: true, data: outcome.data.places.find((p) => p.place_id === placeId) ?? null };
    },
    routeCheck: async (placeId, location) => {
      const outcome = await call((authorization) =>
        client.POST("/v1/routes/check", {
          params: { header: { authorization } },
          body: { schema_version: 1, place_id: placeId, mode: "DRIVE", ...(location ? { location } : {}) },
        }),
      );
      return outcome.ok ? { ok: true, data: outcome.data.route } : outcome;
    },
    propose: ({ effect, target, userLabel, workspaceId, planVersion, idempotencyKey, dueAt, labelKind, position }) =>
      call((authorization) =>
        client.POST("/v1/actions/propose", {
          params: { header: { authorization, "idempotency-key": idempotencyKey } },
          body: {
            schema_version: 1,
            effect,
            target,
            user_label: userLabel,
            workspace_id: workspaceId,
            plan_version: planVersion,
            ...(dueAt ? { due_at: dueAt } : {}),
            ...(labelKind ? { label_kind: labelKind } : {}),
            ...(position ? { position: { latitude: position.latitude, longitude: position.longitude } } : {}),
          },
        }),
      ),
    confirm: (action) =>
      call((authorization) =>
        client.POST("/v1/actions/{id}/confirm", {
          // The same key on every retry of this confirmation; the server replays, never repeats.
          params: { header: { authorization, "idempotency-key": action.idempotency_key }, path: { id: action.id } },
          body: { schema_version: 1, nonce: action.nonce },
        }),
      ),
    cancel: async (actionId) => {
      const outcome = await call((authorization) =>
        client.POST("/v1/actions/{id}/cancel", { params: { header: { authorization }, path: { id: actionId } } }),
      );
      return outcome.ok ? { ok: true, data: null } : outcome;
    },
    recordOutcome: ({ placeId, outcome, note, idempotencyKey, userLabel, intentTag }) =>
      call((authorization) =>
        client.POST("/v1/outcomes", {
          params: { header: { authorization, "idempotency-key": idempotencyKey } },
          body: { schema_version: 1, kind: "outcome_checkin", place_id: placeId, outcome,
            ...(note ? { note } : {}),
            ...(userLabel ? { user_label: userLabel } : {}),
            ...(intentTag ? { intent_tag: intentTag } : {}),
          },
        }),
      ),
    activateTrip: (workspaceId, planVersion) =>
      call((authorization) =>
        client.POST("/v1/trips/{id}:activate", {
          params: { header: { authorization }, path: { id: workspaceId } },
          body: { schema_version: 1, plan_version: planVersion },
        }),
      ),
  };
}

/** Used when no board API is wired (and in tests of the chat alone): everything is "not available yet". */
export const UNAVAILABLE_BOARD_API: BoardApi = (() => {
  const no = async (): Promise<{ ok: false; failure: BoardFailure }> => ({ ok: false, failure: { kind: "unavailable" } });
  return {
    getWorkspace: no,
    editWorkspace: no,
    placeDetails: no,
    routeCheck: no,
    propose: no,
    confirm: no,
    cancel: no,
    recordOutcome: no,
    activateTrip: no,
  };
})();
