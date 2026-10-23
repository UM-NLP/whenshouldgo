// Auth is behind a small interface so a real provider (Firebase anonymous sign-in plus the
// session bootstrap) can replace the DEV one without touching callers. The server owns identity:
// the client only presents a bearer token and never decides a tier or an allowance.

export interface AuthProvider {
  /** A bearer token for the API. Rejects with AuthUnavailable when there is none. */
  getToken(): Promise<string>;
}

export class AuthUnavailable extends Error {
  constructor() {
    super("auth unavailable");
    this.name = "AuthUnavailable";
  }
}

/** The fixed public token that backend/scripts/dev_server.py accepts. Not a credential. */
export const DEV_TOKEN = "dev-only-public-token";

/** NON-PRODUCTION. Sends the dev token. The backend bootstrap route (POST /v1/session:bootstrap) reports usage only; it issues no token. */
export class DevAuthProvider implements AuthProvider {
  async getToken(): Promise<string> {
    return DEV_TOKEN;
  }
}

/** Fail closed: used whenever no real provider exists (every production build today). */
export class NoAuthProvider implements AuthProvider {
  async getToken(): Promise<string> {
    throw new AuthUnavailable();
  }
}

/** The dev provider exists only in a Vite dev build; a production build gets no token at all. */
export function defaultAuthProvider(): AuthProvider {
  return import.meta.env.DEV ? new DevAuthProvider() : new NoAuthProvider();
}
