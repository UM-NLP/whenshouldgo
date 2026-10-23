import createClient from "openapi-fetch";

import type { AuthProvider } from "../auth";
import { AuthUnavailable } from "../auth";
import type { Coordinates } from "../lib/geo";
import { classifyFailure, type ErrorBody, type Failure } from "./failures";
import type { components, paths } from "./schema";

export type TurnResponse = components["schemas"]["TurnResponse"];
export type DiscoveryBlock = components["schemas"]["DiscoveryBlock"];
export type DiscoveryPlace = components["schemas"]["DiscoveryPlace"];
export type TurnWarning = components["schemas"]["TurnWarning"];
export type UsageView = components["schemas"]["UsageView"];
export type SessionBootstrap = components["schemas"]["SessionBootstrap"];

export type Outcome<T> = { ok: true; data: T } | { ok: false; failure: Failure };

export interface TurnInput {
  message: string;
  idempotencyKey: string;
  location?: Coordinates | null;
}

/** Everything the UI may ask of the backend. Types come from the generated OpenAPI schema. */
export interface Api {
  sendTurn(input: TurnInput): Promise<Outcome<TurnResponse>>;
  /** Idempotent session start; returns the server-computed usage view. */
  bootstrap(): Promise<Outcome<SessionBootstrap>>;
  grantLocationConsent(): Promise<Outcome<null>>;
  withdrawLocationConsent(): Promise<Outcome<null>>;
  hasLocationConsent(): Promise<Outcome<boolean>>;
}

/** The notice version the server requires for LOCATION_FOREGROUND (backend/app/consent.py). */
export const LOCATION_NOTICE_VERSION = "location-notice-1";

export interface ApiOptions {
  auth: AuthProvider;
  fetchImpl?: typeof fetch;
  baseUrl?: string;
}

interface Raw<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

export function createApi({ auth, fetchImpl, baseUrl }: ApiOptions): Api {
  const client = createClient<paths>({
    baseUrl: baseUrl ?? globalThis.location.origin,
    // Looked up at call time so a test can swap fetch. Cookies are never sent.
    fetch: (request: Request) => (fetchImpl ?? globalThis.fetch)(request),
    credentials: "omit",
  });

  async function call<T, R>(
    run: (authorizationHeader: string) => Promise<Raw<T>>,
    onSuccess: (data: T | undefined) => R | null,
  ): Promise<Outcome<R>> {
    let header: string;
    try {
      header = `Bearer ${await auth.getToken()}`;
    } catch (error) {
      return { ok: false, failure: error instanceof AuthUnavailable ? { kind: "auth" } : { kind: "unknown" } };
    }
    try {
      const { data, error, response } = await run(header);
      if (response.ok) {
        const value = onSuccess(data);
        return value === null ? { ok: false, failure: { kind: "unknown" } } : { ok: true, data: value };
      }
      return {
        ok: false,
        failure: classifyFailure(
          response.status,
          typeof error === "object" && error !== null ? (error as Partial<ErrorBody>) : undefined,
          response.headers.get("retry-after"),
        ),
      };
    } catch {
      // A network failure or an unparseable body: the exception text is never kept or shown.
      return { ok: false, failure: { kind: "network" } };
    }
  }

  const nothing = (): null | "" => "";

  return {
    bootstrap: () =>
      call(
        (authorization) => client.POST("/v1/session:bootstrap", { params: { header: { authorization } } }),
        (data) => data ?? null,
      ),
    sendTurn: ({ message, idempotencyKey, location }) =>
      call(
        (authorization) =>
          client.POST("/v1/agent/turns", {
            params: { header: { authorization, "idempotency-key": idempotencyKey } },
            body: location ? { message, location } : { message },
          }),
        (data) => data ?? null,
      ),
    grantLocationConsent: async () => {
      const outcome = await call(
        (authorization) =>
          client.POST("/v1/me/consents", {
            params: { header: { authorization } },
            body: { purpose: "LOCATION_FOREGROUND", notice_version: LOCATION_NOTICE_VERSION },
          }),
        nothing,
      );
      return outcome.ok ? { ok: true, data: null } : outcome;
    },
    withdrawLocationConsent: async () => {
      const outcome = await call(
        (authorization) =>
          client.DELETE("/v1/me/consents/{purpose}", {
            params: { header: { authorization }, path: { purpose: "LOCATION_FOREGROUND" } },
          }),
        nothing,
      );
      return outcome.ok ? { ok: true, data: null } : outcome;
    },
    hasLocationConsent: async () => {
      const outcome = await call(
        (authorization) => client.GET("/v1/me/consents", { params: { header: { authorization } } }),
        (data) => data ?? null,
      );
      return outcome.ok
        ? { ok: true, data: outcome.data.consents.some((c) => c.purpose === "LOCATION_FOREGROUND") }
        : outcome;
    },
  };
}
