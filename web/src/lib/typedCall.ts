// A small shared wrapper over the generated openapi-fetch client: attaches the bearer header from an
// AuthProvider at call time, and turns every failure into a typed value chosen by a classifier.
// A server message, a token or a response body never leaves this function as text.
import createClient from "openapi-fetch";

import type { ErrorBody } from "../api/failures";
import type { paths } from "../api/schema";
import { AuthUnavailable, type AuthProvider } from "../auth";

export interface CallerOptions {
  auth: AuthProvider;
  fetchImpl?: typeof fetch;
  baseUrl?: string;
}

interface Raw<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

export type Result<T, F> = { ok: true; data: T } | { ok: false; failure: F };

export function createCaller<F extends { kind: string }>(
  { auth, fetchImpl, baseUrl }: CallerOptions,
  classify: (status: number, body: Partial<ErrorBody> | undefined, retryAfter: string | null) => F,
  authFailure: F,
  networkFailure: F,
  unknownFailure: F,
) {
  const client = createClient<paths>({
    baseUrl: baseUrl ?? globalThis.location.origin,
    fetch: (request: Request) => (fetchImpl ?? globalThis.fetch)(request),
    credentials: "omit",
  });

  async function call<T>(run: (authorization: string) => Promise<Raw<T>>): Promise<Result<T, F>> {
    let header: string;
    try {
      header = `Bearer ${await auth.getToken()}`;
    } catch (error) {
      return { ok: false, failure: error instanceof AuthUnavailable ? authFailure : unknownFailure };
    }
    try {
      const { data, error, response } = await run(header);
      if (response.ok) {
        return data === undefined ? { ok: false, failure: unknownFailure } : { ok: true, data };
      }
      return {
        ok: false,
        failure: classify(
          response.status,
          typeof error === "object" && error !== null ? (error as Partial<ErrorBody>) : undefined,
          response.headers.get("retry-after"),
        ),
      };
    } catch {
      return { ok: false, failure: networkFailure };
    }
  }

  return { client, call };
}
