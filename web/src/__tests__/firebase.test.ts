import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AuthUnavailable } from "../auth";
import {
  AuthServiceError,
  FirebaseAuthService,
  NotConfiguredAuthService,
  createAuthService,
  mapFirebaseError,
  providerFromService,
  readFirebaseConfig,
} from "../firebase";
import { DevAuthService } from "../firebaseDev";

const loads = vi.hoisted(() => ({ app: 0, auth: 0 }));
const sdk = vi.hoisted(() => {
  const user = { getIdToken: vi.fn(async () => "tok-1") };
  const auth = { currentUser: null as null | typeof user, authStateReady: vi.fn(async () => undefined) };
  return {
    user,
    auth,
    signInAnonymously: vi.fn(async (a: typeof auth) => {
      a.currentUser = user;
    }),
    linkWithPopup: vi.fn(async () => undefined as unknown),
    linkWithCredential: vi.fn(async () => undefined as unknown),
    signInWithCredential: vi.fn(async () => undefined),
    sendSignInLinkToEmail: vi.fn(async () => undefined),
    isSignInWithEmailLink: vi.fn(() => true),
    signOut: vi.fn(async () => undefined),
    credentialFromError: vi.fn(() => ({ providerId: "google.com" })),
    credentialWithLink: vi.fn(() => ({ providerId: "emailLink" })),
  };
});

vi.mock("firebase/app", () => {
  loads.app += 1;
  return { initializeApp: vi.fn(() => ({})), getApps: vi.fn(() => []) };
});
vi.mock("firebase/auth", () => {
  loads.auth += 1;
  class GoogleAuthProvider {
    static credentialFromError = sdk.credentialFromError;
  }
  return {
    getAuth: () => sdk.auth,
    signInAnonymously: sdk.signInAnonymously,
    linkWithPopup: sdk.linkWithPopup,
    linkWithCredential: sdk.linkWithCredential,
    signInWithCredential: sdk.signInWithCredential,
    sendSignInLinkToEmail: sdk.sendSignInLinkToEmail,
    isSignInWithEmailLink: sdk.isSignInWithEmailLink,
    signOut: sdk.signOut,
    GoogleAuthProvider,
    EmailAuthProvider: { credentialWithLink: sdk.credentialWithLink },
  };
});

const CONFIG = { apiKey: "k", authDomain: "d", projectId: "p", appId: "a" };

afterEach(() => {
  vi.unstubAllEnvs();
  sdk.auth.currentUser = null;
  vi.clearAllMocks();
});

describe("config", () => {
  it("reads config only when all four VITE_FIREBASE_* values are present", () => {
    expect(readFirebaseConfig({})).toBeNull();
    expect(readFirebaseConfig({ VITE_FIREBASE_API_KEY: "k", VITE_FIREBASE_AUTH_DOMAIN: "d", VITE_FIREBASE_PROJECT_ID: "p" })).toBeNull();
    expect(readFirebaseConfig({ VITE_FIREBASE_API_KEY: "k", VITE_FIREBASE_AUTH_DOMAIN: "d", VITE_FIREBASE_PROJECT_ID: "p", VITE_FIREBASE_APP_ID: "  " })).toBeNull();
    expect(readFirebaseConfig({ VITE_FIREBASE_API_KEY: "k", VITE_FIREBASE_AUTH_DOMAIN: "d", VITE_FIREBASE_PROJECT_ID: "p", VITE_FIREBASE_APP_ID: "a" })).toEqual(CONFIG);
  });

  it("without config: dev fake in dev, the fixed not-configured service in production, never the SDK", async () => {
    expect(createAuthService(null)).toBeInstanceOf(DevAuthService);
    vi.stubEnv("DEV", false);
    const prod = createAuthService(null);
    expect(prod).toBeInstanceOf(NotConfiguredAuthService);
    expect(prod.mode).toBe("not_configured");
    await expect(prod.getIdToken()).rejects.toMatchObject({ kind: "not_configured" });
    await expect(prod.linkWithGoogle()).rejects.toBeInstanceOf(AuthServiceError);
    await expect(providerFromService(prod).getToken()).rejects.toBeInstanceOf(AuthUnavailable);
    expect(loads.app + loads.auth).toBe(0); // the SDK is not even imported
  });

  it("with config it is the Firebase service, and the SDK still waits for first use", () => {
    vi.stubEnv("DEV", false);
    expect(createAuthService(CONFIG)).toBeInstanceOf(FirebaseAuthService);
    expect(loads.app + loads.auth).toBe(0);
  });
});

describe("FirebaseAuthService (SDK mocked)", () => {
  it("signs in anonymously on first token use and returns the token", async () => {
    const service = new FirebaseAuthService(CONFIG);
    expect(await service.getIdToken()).toBe("tok-1");
    expect(sdk.signInAnonymously).toHaveBeenCalledTimes(1);
    await service.getIdToken();
    expect(sdk.signInAnonymously).toHaveBeenCalledTimes(1);
  });

  it("links Google and refreshes the token the server will check", async () => {
    const service = new FirebaseAuthService(CONFIG);
    await service.linkWithGoogle();
    expect(sdk.linkWithPopup).toHaveBeenCalledTimes(1);
    expect(sdk.user.getIdToken).toHaveBeenCalledWith(true);
  });

  it("a collision is a typed error and continueAsExisting signs in with that credential", async () => {
    sdk.linkWithPopup.mockRejectedValueOnce({ code: "auth/credential-already-in-use", message: "someone@example.com owns it" });
    const service = new FirebaseAuthService(CONFIG);
    const error = await service.linkWithGoogle().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(AuthServiceError);
    expect((error as AuthServiceError).kind).toBe("collision");
    expect((error as Error).message).not.toContain("someone@example.com");
    await service.continueAsExisting();
    expect(sdk.signInWithCredential).toHaveBeenCalledTimes(1);
    await expect(service.continueAsExisting()).rejects.toMatchObject({ kind: "unknown" }); // the credential is used once
  });

  it("email link: the continue URL carries no email, and finishing links the credential", async () => {
    const service = new FirebaseAuthService(CONFIG);
    await service.sendEmailLink("canary.person@example.com");
    const call = sdk.sendSignInLinkToEmail.mock.calls[0] as unknown as [unknown, string, { url: string; handleCodeInApp: boolean }];
    expect(call[2].handleCodeInApp).toBe(true);
    expect(call[2].url).not.toContain("canary");
    await service.linkWithEmailLink("canary.person@example.com");
    expect(sdk.credentialWithLink).toHaveBeenCalledTimes(1);
    expect(sdk.linkWithCredential).toHaveBeenCalledTimes(1);
  });

  it("checks for an email-link return only when the URL has an action code", async () => {
    const service = new FirebaseAuthService(CONFIG);
    expect(await service.isEmailLinkReturn()).toBe(false);
    expect(sdk.isSignInWithEmailLink).not.toHaveBeenCalled();
    expect(sdk.auth.authStateReady).not.toHaveBeenCalled();
  });

  it("maps SDK error codes to kinds and drops the message", () => {
    expect(mapFirebaseError({ code: "auth/popup-closed-by-user" }).kind).toBe("cancelled");
    expect(mapFirebaseError({ code: "auth/popup-blocked" }).kind).toBe("popup_blocked");
    expect(mapFirebaseError({ code: "auth/network-request-failed" }).kind).toBe("network");
    expect(mapFirebaseError({ code: "auth/invalid-email" }).kind).toBe("invalid_email");
    expect(mapFirebaseError({ code: "auth/expired-action-code" }).kind).toBe("invalid_link");
    expect(mapFirebaseError({ code: "auth/operation-not-allowed" }).kind).toBe("unsupported");
    expect(mapFirebaseError(new Error("anything")).kind).toBe("unknown");
  });

  it("signOut forgets a held credential", async () => {
    const service = new FirebaseAuthService(CONFIG);
    await service.signOut();
    expect(sdk.signOut).toHaveBeenCalledTimes(1);
  });
});

// Needs `npm run build` first (CI builds before testing); skipped when there is no dist.
const DIST = join(__dirname, "..", "..", "dist");
describe.skipIf(!existsSync(DIST))("production bundle", () => {
  it("contains no dev provider, dev token or fake auth service", () => {
    const files: string[] = [];
    const walk = (dir: string) => {
      for (const name of readdirSync(dir, { withFileTypes: true })) {
        const path = join(dir, name.name);
        if (name.isDirectory()) walk(path);
        else if (/\.(js|html|css)$/.test(name.name)) files.push(path);
      }
    };
    walk(DIST);
    expect(files.length).toBeGreaterThan(0);
    for (const file of files) {
      const text = readFileSync(file, "utf8");
      expect(text, file).not.toMatch(/dev-only-public-token|DevAuthProvider|DevAuthService/);
    }
  });
});
