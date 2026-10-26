// NON-PRODUCTION. A fake sign-in for `npm run dev` and for tests. It is only reachable from
// createAuthService() under `import.meta.env.DEV`, so a production build does not contain it.
import { DEV_TOKEN } from "./auth";
import { AuthServiceError, type AuthService } from "./firebase";

export interface DevAuthBehavior {
  /** Make the provider link throw this kind (for example "cancelled" or "collision"). */
  linkFails?: "cancelled" | "collision" | "popup_blocked" | "network" | "unknown";
  /** Pretend the page was opened from an email link. */
  emailReturn?: boolean;
}

export class DevAuthService implements AuthService {
  readonly mode = "dev" as const;
  calls: string[] = [];
  constructor(public behavior: DevAuthBehavior = {}) {}

  private record(name: string): void {
    this.calls.push(name);
  }
  async signInAnonymously(): Promise<void> {
    this.record("signInAnonymously");
  }
  async linkWithGoogle(): Promise<void> {
    this.record("linkWithGoogle");
    if (this.behavior.linkFails) throw new AuthServiceError(this.behavior.linkFails);
  }
  async sendEmailLink(): Promise<void> {
    this.record("sendEmailLink");
    if (this.behavior.linkFails === "network") throw new AuthServiceError("network");
  }
  async isEmailLinkReturn(): Promise<boolean> {
    return this.behavior.emailReturn === true;
  }
  async linkWithEmailLink(): Promise<void> {
    this.record("linkWithEmailLink");
    if (this.behavior.linkFails) throw new AuthServiceError(this.behavior.linkFails);
  }
  async continueAsExisting(): Promise<void> {
    this.record("continueAsExisting");
  }
  async getIdToken(): Promise<string> {
    return DEV_TOKEN;
  }
  async signOut(): Promise<void> {
    this.record("signOut");
  }
}
