import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DEV_TOKEN } from "../auth";
import { NotConfiguredAuthService } from "../firebase";
import { DevAuthService } from "../firebaseDev";
import { acceptLocalNotice, errorBody, json, renderApp } from "./phase1Support";
import { freshStore } from "./idb";
import { usageView } from "./support";

const GATE =
  "You used your 50 guest turns. Create a free account to continue and receive 100 turns every 30 days.";
const gated = () => usageView({ meter_state: "GATED", gate_reason: "TURNS_LIMIT", user_turns_used: 50, registered_turn_limit: 100 });
const INTENT = { schema_version: 1, kind: "account_link_intent", id: "int-1", provider: "GOOGLE", nonce: "nonce-secret-xyz", created_at: "2026-10-26T12:00:00Z", expires_at: "2026-10-26T12:15:00Z" };
const LINKED = { schema_version: 1, kind: "account_link_result", linked: true, tier: "registered" };
const EMAIL = "canary.person@example.com";

function manifest(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: 1,
    kind: "account_migration",
    id: "mig-1",
    state: "PREVIEW",
    completed: false,
    preference: null,
    items: [
      { id: "i1", kind: "workspace", title: "Dinner trip", selected: true, copied: false },
      { id: "i2", kind: "workspace", title: "Hike plan", selected: true, copied: false },
    ],
    ...overrides,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("create a free account action", () => {
  it("shows nothing at NONE, and the action at the 90% notice and at the gate (exact wording kept)", async () => {
    const none = renderApp();
    await waitFor(() => expect(none.calls("POST", "/v1/session:bootstrap")).toHaveLength(1));
    expect(screen.queryByRole("button", { name: "Create a free account" })).toBeNull();
    none.view.unmount();

    const notice = renderApp({ usage: usageView({ meter_state: "NOTICE_90", user_turns_used: 45 }) });
    expect(await screen.findByRole("button", { name: "Create a free account" })).toBeInTheDocument();
    expect(screen.getByText(/recover your trips and keep them in sync/)).toBeInTheDocument();
    notice.view.unmount();

    renderApp({ usage: gated() });
    expect(await screen.findByText(GATE)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create a free account" })).toBeInTheDocument();
  });

  it("is not offered when the server says it cannot link, to registered users, or without the wiring", async () => {
    const a = renderApp({ usage: gated(), canLink: false });
    await screen.findByText(GATE);
    expect(screen.queryByRole("button", { name: "Create a free account" })).toBeNull();
    a.view.unmount();
    const b = renderApp({ usage: gated(), tier: "registered" });
    await waitFor(() => expect(b.calls("POST", "/v1/session:bootstrap")).toHaveLength(1));
    expect(screen.queryByRole("button", { name: "Create a free account" })).toBeNull();
    b.view.unmount();
    renderApp({ usage: gated(), wireAccount: false });
    await screen.findByText(GATE);
    expect(screen.queryByRole("button", { name: "Create a free account" })).toBeNull();
  });
});

describe("registration flow", () => {
  it("links with Google: intent, provider link, complete, then asks the server again", async () => {
    const app = renderApp({
      usage: gated(),
      routes: {
        "POST /v1/account/link-intents": json(INTENT),
        "POST /v1/account/link-intents/int-1:complete": json(LINKED),
      },
    });
    await app.user.click(await screen.findByRole("button", { name: "Create a free account" }));
    await app.user.click(screen.getByRole("button", { name: "Continue with Google" }));
    expect(await screen.findByText(/Your free account is ready/)).toBeInTheDocument();

    const [intent] = app.calls("POST", "/v1/account/link-intents");
    expect(intent?.body).toEqual({ schema_version: 1, provider: "GOOGLE" });
    expect(intent?.headers.get("idempotency-key")).toMatch(/^web-/);
    const [complete] = app.calls("POST", "/v1/account/link-intents/int-1:complete");
    expect(complete?.body).toEqual({ schema_version: 1, nonce: "nonce-secret-xyz" });
    expect((app.authService as DevAuthService).calls).toContain("linkWithGoogle");
    await waitFor(() => expect(app.calls("POST", "/v1/session:bootstrap").length).toBeGreaterThan(1));
    expect(screen.queryByText("nonce-secret-xyz")).toBeNull();
  });

  it("cancelling the provider consumes nothing: no complete call, fixed note", async () => {
    const app = renderApp({
      usage: gated(),
      authBehavior: { linkFails: "cancelled" },
      routes: { "POST /v1/account/link-intents": json(INTENT) },
    });
    await app.user.click(await screen.findByRole("button", { name: "Create a free account" }));
    await app.user.click(screen.getByRole("button", { name: "Continue with Google" }));
    expect(await screen.findByText(/Cancelled\. Nothing was changed/)).toBeInTheDocument();
    expect(app.calls("POST", "/v1/account/link-intents/int-1:complete")).toHaveLength(0);
    await app.user.click(screen.getByRole("button", { name: "Not now" }));
    expect(screen.queryByRole("dialog", { name: "Create a free account" })).toBeNull();
    // The meter and the gate are untouched.
    expect(screen.getByText(GATE)).toBeInTheDocument();
  });

  it("shows a fixed 'not configured' state and sends nothing when sign-in is not configured", async () => {
    const app = renderApp({ usage: gated(), authService: new NotConfiguredAuthService() });
    expect(screen.getAllByText(/Sign-in is not configured in this build/).length).toBeGreaterThan(0);
    await app.user.click(await screen.findByRole("button", { name: "Create a free account" }));
    expect(screen.queryByRole("button", { name: "Continue with Google" })).toBeNull();
    expect(app.calls("POST", "/v1/account/link-intents")).toHaveLength(0);
  });

  it("maps a 501 to 'not available yet', and other failures to fixed copy", async () => {
    const app = renderApp({ usage: gated(), routes: { "POST /v1/account/link-intents": json(errorBody("POLICY", "NOT_IMPLEMENTED", "secret server text"), 501) } });
    await app.user.click(await screen.findByRole("button", { name: "Create a free account" }));
    await app.user.click(screen.getByRole("button", { name: "Continue with Google" }));
    expect(await screen.findByText(/Accounts are not available yet/)).toBeInTheDocument();
    expect(screen.queryByText(/secret server text/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it.each([
    ["LINK_INTENT_EXPIRED", 409, /timed out/],
    ["LINK_INTENT_CONSUMED", 409, /already used/],
    ["WRONG_UID", 403, /different|not the one/],
    ["ANONYMOUS_PROVIDER", 409, /permanent account/],
  ])("shows fixed copy for %s", async (reason, status, text) => {
    const app = renderApp({
      usage: gated(),
      routes: {
        "POST /v1/account/link-intents": json(INTENT),
        "POST /v1/account/link-intents/int-1:complete": json(errorBody("CONFLICT", reason), status),
      },
    });
    await app.user.click(await screen.findByRole("button", { name: "Create a free account" }));
    await app.user.click(screen.getByRole("button", { name: "Continue with Google" }));
    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it("offers a retry for a network failure and repeats the same idempotency key", async () => {
    let first = true;
    const app = renderApp({
      usage: gated(),
      routes: {
        "POST /v1/account/link-intents": () => {
          if (first) {
            first = false;
            return new Error("down");
          }
          return json(INTENT);
        },
        "POST /v1/account/link-intents/int-1:complete": json(LINKED),
      },
    });
    await app.user.click(await screen.findByRole("button", { name: "Create a free account" }));
    await app.user.click(screen.getByRole("button", { name: "Continue with Google" }));
    expect(await screen.findByText(/Could not reach the service/)).toBeInTheDocument();
    await app.user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText(/Your free account is ready/)).toBeInTheDocument();
    const keys = app.calls("POST", "/v1/account/link-intents").map((r) => r.headers.get("idempotency-key"));
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);
  });

  it("email link: sends the link, never puts the email in a request or URL, and finishes on return", async () => {
    const sent = renderApp({ usage: gated(), routes: { "POST /v1/account/link-intents": json({ ...INTENT, provider: "EMAIL_LINK" }) } });
    await sent.user.click(await screen.findByRole("button", { name: "Create a free account" }));
    await sent.user.click(screen.getByRole("button", { name: "Use an email link instead" }));
    await sent.user.type(screen.getByLabelText(/Email address/), EMAIL);
    await sent.user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByText(/Check your inbox/)).toBeInTheDocument();
    expect((sent.authService as DevAuthService).calls).toContain("sendEmailLink");
    expect(sent.calls("POST", "/v1/account/link-intents")).toHaveLength(0); // nothing server-side until the user returns
    sent.view.unmount();

    const back = renderApp({
      usage: gated(),
      authBehavior: { emailReturn: true },
      routes: {
        "POST /v1/account/link-intents": json({ ...INTENT, provider: "EMAIL_LINK" }),
        "POST /v1/account/link-intents/int-1:complete": json(LINKED),
      },
    });
    expect(await screen.findByText(/Enter the email address you used/)).toBeInTheDocument();
    await back.user.type(screen.getByLabelText(/Email address/), EMAIL);
    await back.user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByText(/Your free account is ready/)).toBeInTheDocument();
    expect(back.calls("POST", "/v1/account/link-intents")[0]?.body).toEqual({ schema_version: 1, provider: "EMAIL_LINK" });
    for (const r of [...sent.requests, ...back.requests]) {
      expect(JSON.stringify(r.body ?? "") + r.url).not.toContain("canary.person");
    }
    expect(window.location.href).not.toContain("canary.person");
  });
});

describe("merge after a collision", () => {
  const collisionRoutes = (extra: Record<string, unknown> = {}) => ({
    "POST /v1/account/link-intents": json(INTENT),
    "POST /v1/account/link-intents/int-1:complete": json(errorBody("CONFLICT", "ACCOUNT_COLLISION"), 409),
    "POST /v1/account-migrations": json(manifest()),
    ...extra,
  });

  it("shows the manifest with KEEP_REGISTERED by default, runs it, and resumes after an interruption", async () => {
    let runs = 0;
    const app = renderApp({
      usage: gated(),
      authBehavior: { linkFails: "collision" },
      routes: collisionRoutes({
        "POST /v1/account-migrations/mig-1:run": () => {
          runs += 1;
          if (runs === 1) return json(errorBody("PROVIDER", "MIGRATION_INTERRUPTED"), 503);
          return json(manifest({ state: "COMPLETED", completed: true, items: manifest().items.map((i) => ({ ...i, copied: true })) }));
        },
        "GET /v1/account-migrations/mig-1": json(manifest({ state: "RUNNING", items: [{ ...manifest().items[0]!, copied: true }, manifest().items[1]!] })),
      }),
    });
    await app.user.click(await screen.findByRole("button", { name: "Create a free account" }));
    await app.user.click(screen.getByRole("button", { name: "Continue with Google" }));

    expect(await screen.findByRole("heading", { name: "Combine with your existing account" })).toBeInTheDocument();
    expect(screen.getByLabelText("Dinner trip")).toBeChecked();
    expect(screen.getByLabelText("Hike plan")).toBeChecked();
    expect(screen.getByLabelText(/Keep my account's saved settings/)).toBeChecked();
    expect((app.authService as DevAuthService).calls).toContain("continueAsExisting");
    // The intent and its nonce prove the guest; the migration starts only after the user is shown it.
    expect(app.calls("POST", "/v1/account-migrations")[0]?.body).toEqual({ schema_version: 1, intent_id: "int-1", nonce: "nonce-secret-xyz" });
    expect(app.calls("POST", "/v1/account-migrations/mig-1:run")).toHaveLength(0);

    await app.user.click(screen.getByLabelText("Hike plan")); // deselect one
    await app.user.click(screen.getByRole("button", { name: "Copy selected trips" }));
    expect(await screen.findByText(/Copying was interrupted/)).toBeInTheDocument();
    expect(screen.getByText("(copied)")).toBeInTheDocument();
    await app.user.click(screen.getByRole("button", { name: "Continue copying" }));
    expect(await screen.findByText(/All chosen trips were copied/)).toBeInTheDocument();

    const bodies = app.calls("POST", "/v1/account-migrations/mig-1:run").map((r) => r.body);
    expect(bodies).toHaveLength(2);
    expect(bodies[0]).toEqual({ schema_version: 1, item_ids: ["i1"], preference: "KEEP_REGISTERED" });
    expect(bodies[1]).toEqual(bodies[0]); // the same call resumes it
    await app.user.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(app.calls("POST", "/v1/session:bootstrap").length).toBeGreaterThan(1));
  });

  it("lets the user choose USE_GUEST, and 'Stop for now' leaves with a clear note and no run", async () => {
    const app = renderApp({
      usage: gated(),
      authBehavior: { linkFails: "collision" },
      routes: collisionRoutes({
        "POST /v1/account-migrations/mig-1:run": json(manifest({ state: "COMPLETED", completed: true })),
      }),
    });
    await app.user.click(await screen.findByRole("button", { name: "Create a free account" }));
    await app.user.click(screen.getByRole("button", { name: "Continue with Google" }));
    await app.user.click(await screen.findByLabelText(/Use my guest settings instead/));
    await app.user.click(screen.getByRole("button", { name: "Copy selected trips" }));
    await screen.findByText(/All chosen trips were copied/);
    expect(app.calls("POST", "/v1/account-migrations/mig-1:run")[0]?.body).toMatchObject({ preference: "USE_GUEST" });

    app.view.unmount();
    const stop = renderApp({ usage: gated(), authBehavior: { linkFails: "collision" }, routes: collisionRoutes() });
    await stop.user.click(await screen.findByRole("button", { name: "Create a free account" }));
    await stop.user.click(screen.getByRole("button", { name: "Continue with Google" }));
    await screen.findByRole("heading", { name: "Combine with your existing account" });
    expect(screen.getByText(/you stay signed in to your existing account/)).toBeInTheDocument();
    await stop.user.click(screen.getByRole("button", { name: "Stop for now" }));
    expect(screen.queryByRole("heading", { name: "Combine with your existing account" })).toBeNull();
    expect(stop.calls("POST", "/v1/account-migrations/mig-1:run")).toHaveLength(0);
  });

  it("also enters the merge when the SERVER reports the collision on complete", async () => {
    const app = renderApp({ usage: gated(), routes: collisionRoutes() });
    await app.user.click(await screen.findByRole("button", { name: "Create a free account" }));
    await app.user.click(screen.getByRole("button", { name: "Continue with Google" }));
    expect(await screen.findByRole("heading", { name: "Combine with your existing account" })).toBeInTheDocument();
  });

  it("shows a start failure with a retry", async () => {
    let calls = 0;
    const app = renderApp({
      usage: gated(),
      authBehavior: { linkFails: "collision" },
      routes: collisionRoutes({
        "POST /v1/account-migrations": () => (++calls === 1 ? json(errorBody("POLICY", "ANONYMOUS_PROVIDER"), 500) : json(manifest())),
      }),
    });
    await app.user.click(await screen.findByRole("button", { name: "Create a free account" }));
    await app.user.click(screen.getByRole("button", { name: "Continue with Google" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });
});

describe("export and delete while GATED", () => {
  it("exports the server data as a file built from the response, never stored", async () => {
    const created: Blob[] = [];
    vi.spyOn(URL, "createObjectURL").mockImplementation((b) => {
      created.push(b as Blob);
      return "blob:x";
    });
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    const app = renderApp({
      usage: gated(),
      routes: { "POST /v1/me:export": json({ schema_version: 1, kind: "export_bundle", tier: "guest", outings: [], saved_places: [], marker: "EXPORT-MARKER" }) },
    });
    await screen.findByText(GATE);
    expect(screen.getByLabelText("Your message")).toBeDisabled();
    await app.user.click(screen.getByRole("button", { name: "Export my account data" }));
    expect(await screen.findByText("Exported.")).toBeInTheDocument();
    expect(created).toHaveLength(1);
    expect(await created[0]!.text()).toContain("EXPORT-MARKER");
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("shows 'not available yet' when the server cannot export", async () => {
    const app = renderApp({ usage: gated(), routes: { "POST /v1/me:export": json(errorBody("PROVIDER", "SERVER_NOT_CONFIGURED"), 503) } });
    await app.user.click(await screen.findByRole("button", { name: "Export my account data" }));
    expect(await screen.findByText(/not available yet/)).toBeInTheDocument();
  });

  it("deletes after a confirm dialog, then clears every local store and signs out", async () => {
    const store = freshStore();
    const app = renderApp({
      usage: gated(),
      local: store,
      routes: { "POST /v1/me:delete": json({ schema_version: 1, kind: "delete_receipt", completed: true, requested_at: "2026-10-26T12:00:00Z" }) },
    });
    await acceptLocalNotice(app.user);
    await store.put("plans", { schema_version: 1, id: "p1", place_id: "ChIJabc", label: "Mine", accepted_at: "2026-10-26T12:00:00Z" });
    await app.user.click(await screen.findByRole("button", { name: "Delete my account data" }));
    const dialog = screen.getByRole("alertdialog", { name: "Delete my account data" });
    expect(app.calls("POST", "/v1/me:delete")).toHaveLength(0); // nothing until confirmed
    await app.user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(app.calls("POST", "/v1/me:delete")).toHaveLength(0);

    await app.user.click(screen.getByRole("button", { name: "Delete my account data" }));
    await app.user.click(screen.getByRole("button", { name: "Yes, delete everything" }));
    expect(await screen.findByText(/deleted from the server and from this device/)).toBeInTheDocument();
    const all = (await store.exportAll()).stores;
    for (const rows of Object.values(all)) expect(rows).toEqual([]);
    expect((app.authService as DevAuthService).calls).toContain("signOut");
  });

  it("an unfinished delete says so and can be retried without clearing the device first", async () => {
    const store = freshStore();
    let done = false;
    const app = renderApp({
      usage: gated(),
      local: store,
      routes: {
        "POST /v1/me:delete": () => json({ schema_version: 1, kind: "delete_receipt", completed: done, requested_at: "2026-10-26T12:00:00Z" }),
      },
    });
    await acceptLocalNotice(app.user);
    await store.put("plans", { schema_version: 1, id: "p1", place_id: "ChIJabc", label: "Mine", accepted_at: "2026-10-26T12:00:00Z" });
    await app.user.click(await screen.findByRole("button", { name: "Delete my account data" }));
    await app.user.click(screen.getByRole("button", { name: "Yes, delete everything" }));
    expect(await screen.findByText(/Deleting is not finished yet/)).toBeInTheDocument();
    expect((await store.getAll("plans")).length).toBe(1);
    done = true;
    await app.user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText(/deleted from the server and from this device/)).toBeInTheDocument();
    expect(await store.getAll("plans")).toEqual([]);
  });
});

describe("secrets stay out of storage, URLs, logs and the screen", () => {
  it("no token, nonce or email in IndexedDB, web storage, request URLs, console or text", async () => {
    const consoleSpies = (["log", "info", "warn", "error", "debug"] as const).map((m) => vi.spyOn(console, m).mockImplementation(() => undefined));
    const store = freshStore();
    const app = renderApp({
      usage: gated(),
      local: store,
      authBehavior: { linkFails: "collision" },
      routes: {
        "POST /v1/account/link-intents": json(INTENT),
        "POST /v1/account/link-intents/int-1:complete": json(errorBody("CONFLICT", "ACCOUNT_COLLISION"), 409),
        "POST /v1/account-migrations": json(manifest()),
        "POST /v1/account-migrations/mig-1:run": json(manifest({ state: "COMPLETED", completed: true })),
      },
    });
    await acceptLocalNotice(app.user);
    await app.user.click(await screen.findByRole("button", { name: "Create a free account" }));
    await app.user.click(screen.getByRole("button", { name: "Continue with Google" }));
    await app.user.click(await screen.findByRole("button", { name: "Copy selected trips" }));
    await screen.findByText(/All chosen trips were copied/);

    const secrets = [DEV_TOKEN, "nonce-secret-xyz", "canary.person"];
    const idb = JSON.stringify(await store.exportAll());
    const html = document.body.innerHTML;
    for (const secret of secrets) {
      expect(idb).not.toContain(secret);
      expect(html).not.toContain(secret);
      expect(JSON.stringify({ ...localStorage, ...sessionStorage })).not.toContain(secret);
      expect(window.location.href).not.toContain(secret);
      for (const r of app.requests) expect(r.url).not.toContain(secret);
    }
    for (const spy of consoleSpies) expect(spy).not.toHaveBeenCalled();
  });
});
