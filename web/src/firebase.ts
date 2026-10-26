// Sign-in behind one interface (docs/access-tiers.md, docs/runbooks/firebase-setup.md).
//
// Three implementations:
//   - FirebaseAuthService: the real `firebase` web SDK, loaded lazily and ONLY when the VITE_FIREBASE_*
//     config is present. There is no real config in the repository (owner TODO).
//   - NotConfiguredAuthService: a production build with no config. Nothing can sign in; the UI shows a
//     fixed "sign-in not configured" state and the client sends no token.
//   - DevAuthService (./firebaseDev.ts): DEV only. It is referenced under `import.meta.env.DEV`, so a
//     production build removes it. A test greps dist/ for it.
//
// The server owns identity. This file never decides a tier, and a token is returned to the caller
// that asked for it and is never logged, stored by this code, or placed in a URL. (The Firebase SDK
// keeps its own session in its own browser storage; that is how an anonymous uid survives a reload.)
import { AuthUnavailable, type AuthProvider } from "./auth";
import { DevAuthService } from "./firebaseDev";

export type AuthMode = "firebase" | "dev" | "not_configured";

export type AuthServiceFailureKind =
  | "not_configured"
  | "cancelled"
  | "popup_blocked"
  | "collision"
  | "network"
  | "invalid_email"
  | "invalid_link"
  | "unsupported"
  | "unknown";

/** Carries a kind only. The SDK's message (which can contain an email) is dropped at the boundary. */
export class AuthServiceError extends Error {
  constructor(readonly kind: AuthServiceFailureKind) {
    super(`auth service: ${kind}`);
    this.name = "AuthServiceError";
  }
}

export interface AuthService {
  readonly mode: AuthMode;
  /** Idempotent: keeps the current anonymous user, or creates one. */
  signInAnonymously(): Promise<void>;
  /** Links the CURRENT (anonymous) user to Google. Throws AuthServiceError("collision") when the credential already owns an account. */
  linkWithGoogle(): Promise<void>;
  /** Step 1 of the email-link path: sends the link. The email is held in memory for this call only. */
  sendEmailLink(email: string): Promise<void>;
  /** True when this page was opened from an email sign-in link (checked without loading the SDK otherwise). */
  isEmailLinkReturn(): Promise<boolean>;
  /** Step 2: finishes the link on the page the email opened. */
  linkWithEmailLink(email: string): Promise<void>;
  /** After a collision: sign in as the existing registered account with the credential that collided. */
  continueAsExisting(): Promise<void>;
  /** A bearer token. Rejects with AuthServiceError("not_configured") when there is no sign-in. */
  getIdToken(forceRefresh?: boolean): Promise<string>;
  signOut(): Promise<void>;
}

export interface FirebaseConfig {
  apiKey: string;
  authDomain: string;
  projectId: string;
  appId: string;
}

/** Config comes only from the build environment. All four values must be present. */
export function readFirebaseConfig(env: Record<string, unknown> = import.meta.env): FirebaseConfig | null {
  const get = (name: string): string => {
    const value = env[name];
    return typeof value === "string" ? value.trim() : "";
  };
  const config = {
    apiKey: get("VITE_FIREBASE_API_KEY"),
    authDomain: get("VITE_FIREBASE_AUTH_DOMAIN"),
    projectId: get("VITE_FIREBASE_PROJECT_ID"),
    appId: get("VITE_FIREBASE_APP_ID"),
  };
  return Object.values(config).every((v) => v.length > 0) ? config : null;
}

export class NotConfiguredAuthService implements AuthService {
  readonly mode = "not_configured" as const;
  private no(): never {
    throw new AuthServiceError("not_configured");
  }
  async signInAnonymously(): Promise<void> {
    this.no();
  }
  async linkWithGoogle(): Promise<void> {
    this.no();
  }
  async sendEmailLink(): Promise<void> {
    this.no();
  }
  async isEmailLinkReturn(): Promise<boolean> {
    return false;
  }
  async linkWithEmailLink(): Promise<void> {
    this.no();
  }
  async continueAsExisting(): Promise<void> {
    this.no();
  }
  async getIdToken(): Promise<string> {
    return this.no();
  }
  async signOut(): Promise<void> {
    /* nothing to sign out of */
  }
}

type AuthModule = typeof import("firebase/auth");
type FirebaseAuth = import("firebase/auth").Auth;
type AuthCredential = import("firebase/auth").AuthCredential;
interface Sdk {
  mod: AuthModule;
  auth: FirebaseAuth;
}

const CANCELLED = new Set(["auth/popup-closed-by-user", "auth/cancelled-popup-request", "auth/user-cancelled"]);
const COLLISIONS = new Set(["auth/credential-already-in-use", "auth/email-already-in-use"]);

/** Maps an SDK error to a kind. Only `code` is read; the message is never kept. */
export function mapFirebaseError(error: unknown): AuthServiceError {
  if (error instanceof AuthServiceError) return error;
  const code = typeof error === "object" && error !== null && "code" in error ? String((error as { code: unknown }).code) : "";
  if (CANCELLED.has(code)) return new AuthServiceError("cancelled");
  if (code === "auth/popup-blocked") return new AuthServiceError("popup_blocked");
  if (COLLISIONS.has(code)) return new AuthServiceError("collision");
  if (code === "auth/network-request-failed") return new AuthServiceError("network");
  if (code === "auth/invalid-email" || code === "auth/missing-email") return new AuthServiceError("invalid_email");
  if (code === "auth/invalid-action-code" || code === "auth/expired-action-code") return new AuthServiceError("invalid_link");
  if (code === "auth/operation-not-allowed" || code === "auth/unauthorized-domain" || code === "auth/operation-not-supported-in-this-environment") {
    return new AuthServiceError("unsupported");
  }
  return new AuthServiceError("unknown");
}

/** The real implementation. The SDK module is imported on first use, never at startup. */
export class FirebaseAuthService implements AuthService {
  readonly mode = "firebase" as const;
  private sdk: Promise<Sdk> | null = null;
  /** The credential that collided, kept in memory only so the user can continue as the existing account. */
  private collided: AuthCredential | null = null;

  constructor(private readonly config: FirebaseConfig) {}

  private load(): Promise<Sdk> {
    const loading = (this.sdk ??= (async () => {
      const [{ initializeApp, getApps }, mod] = await Promise.all([import("firebase/app"), import("firebase/auth")]);
      const app = getApps()[0] ?? initializeApp(this.config);
      return { mod, auth: mod.getAuth(app) };
    })());
    loading.catch(() => {
      this.sdk = null;
    });
    return loading;
  }

  private async user(sdk: Sdk) {
    await sdk.auth.authStateReady();
    if (!sdk.auth.currentUser) await sdk.mod.signInAnonymously(sdk.auth);
    const user = sdk.auth.currentUser;
    if (!user) throw new AuthServiceError("unknown");
    return user;
  }

  async signInAnonymously(): Promise<void> {
    try {
      await this.user(await this.load());
    } catch (error) {
      throw mapFirebaseError(error);
    }
  }

  async linkWithGoogle(): Promise<void> {
    try {
      const sdk = await this.load();
      const user = await this.user(sdk);
      try {
        await sdk.mod.linkWithPopup(user, new sdk.mod.GoogleAuthProvider());
      } catch (error) {
        const mapped = mapFirebaseError(error);
        if (mapped.kind === "collision") this.collided = sdk.mod.GoogleAuthProvider.credentialFromError(error as never);
        throw mapped;
      }
      await user.getIdToken(true); // the server needs the refreshed token that shows the new provider
    } catch (error) {
      throw mapFirebaseError(error);
    }
  }

  async sendEmailLink(email: string): Promise<void> {
    try {
      const sdk = await this.load();
      // The continue URL carries no email. The user types it again on return.
      const url = `${globalThis.location.origin}${globalThis.location.pathname}`;
      await sdk.mod.sendSignInLinkToEmail(sdk.auth, email, { url, handleCodeInApp: true });
    } catch (error) {
      throw mapFirebaseError(error);
    }
  }

  async isEmailLinkReturn(): Promise<boolean> {
    if (!/[?&]oobCode=/.test(globalThis.location.search)) return false;
    try {
      const sdk = await this.load();
      return sdk.mod.isSignInWithEmailLink(sdk.auth, globalThis.location.href);
    } catch {
      return false;
    }
  }

  async linkWithEmailLink(email: string): Promise<void> {
    try {
      const sdk = await this.load();
      const user = await this.user(sdk);
      const credential = sdk.mod.EmailAuthProvider.credentialWithLink(email, globalThis.location.href);
      try {
        await sdk.mod.linkWithCredential(user, credential);
      } catch (error) {
        const mapped = mapFirebaseError(error);
        if (mapped.kind === "collision") this.collided = credential;
        throw mapped;
      }
      await user.getIdToken(true);
    } catch (error) {
      throw mapFirebaseError(error);
    }
  }

  async continueAsExisting(): Promise<void> {
    try {
      const sdk = await this.load();
      const credential = this.collided;
      if (!credential) throw new AuthServiceError("unknown");
      this.collided = null;
      await sdk.mod.signInWithCredential(sdk.auth, credential);
    } catch (error) {
      throw mapFirebaseError(error);
    }
  }

  async getIdToken(forceRefresh = false): Promise<string> {
    try {
      const sdk = await this.load();
      return await (await this.user(sdk)).getIdToken(forceRefresh);
    } catch (error) {
      throw mapFirebaseError(error);
    }
  }

  async signOut(): Promise<void> {
    this.collided = null;
    try {
      const sdk = await this.load();
      await sdk.mod.signOut(sdk.auth);
    } catch (error) {
      throw mapFirebaseError(error);
    }
  }
}

/** Real SDK when configured; the dev fake only in a Vite dev build; otherwise "not configured". */
export function createAuthService(config: FirebaseConfig | null = readFirebaseConfig()): AuthService {
  if (config) return new FirebaseAuthService(config);
  if (import.meta.env.DEV) return new DevAuthService();
  return new NotConfiguredAuthService();
}

/** The `AuthProvider` the typed API clients use: always asks the service for the current token. */
export function providerFromService(service: AuthService): AuthProvider {
  return {
    async getToken(): Promise<string> {
      try {
        return await service.getIdToken();
      } catch {
        throw new AuthUnavailable();
      }
    },
  };
}
