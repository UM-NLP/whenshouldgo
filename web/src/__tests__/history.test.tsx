import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { askSignalFromTurn } from "../history/contractAdapter";
import { DECLINE_COOLDOWN_DAYS, HOME_QUESTION, WORK_QUESTION, cooldownActive, detectNearKind, shouldAsk } from "../places/SavedAskPolicy";
import { SCHEMA_VERSION, round3, type LocalStore } from "../storage";
import { placeCard } from "./boardSupport";
import { freshStore } from "./idb";
import { CONSENT, errorBody, json, pendingAction, renderApp, sendText, turnOnLocation, turnResponse } from "./phase1Support";
import { mockGeolocation, type Handler } from "./support";

const HEADING = "Your history and saved places";
const DAY = 86_400_000;
const iso = (msDelta: number) => new Date(Date.now() + msDelta).toISOString();

async function seededStore(): Promise<LocalStore> {
  const store = freshStore();
  await store.setNotice(true);
  return store;
}
async function addOuting(store: LocalStore, id: string, over: Record<string, unknown> = {}) {
  await store.put("outing_log", { schema_version: SCHEMA_VERSION, id, place_id: `ChIJ${id}`, label: "Date night spot", activity_tag: "FOOD_DRINK", at: iso(-2 * DAY), outcome: "WENT", ...over } as never);
}
async function addHome(store: LocalStore) {
  await store.put("saved_places", { schema_version: SCHEMA_VERSION, id: "home", place_id: "here-abc", label: "Home", kind: "HOME", position: { lat: 48.858, lng: 2.294 }, updated_at: iso(0) });
}
const openPanel = async (user: ReturnType<typeof renderApp>["user"]) => user.click(await screen.findByRole("button", { name: "Show my history and saved places" }));

function serverOuting(over: Record<string, unknown> = {}) {
  return {
    schema_version: 1,
    kind: "outing_record",
    id: "o-1",
    owner_id: "u",
    workspace_id: "ws-1",
    plan_version: 1,
    occurred_at: iso(-DAY),
    tz: "UTC",
    local_dow: 1,
    local_daypart: "EVENING",
    place_ref: { place_id: "ChIJserver1" },
    user_label: "Server dinner",
    intent_tag: "FOOD_DRINK",
    events: [],
    feedback: {},
    strength: "SELF_REPORTED_VISIT",
    consent_id: "c1",
    retention_expires_at: iso(30 * DAY),
    ...over,
  };
}
function serverSaved(over: Record<string, unknown> = {}) {
  return { schema_version: 1, kind: "saved_place", place_id: "here-srv", user_label: "Home", label_kind: "HOME", position: { latitude: 48.858, longitude: 2.294 }, created_at: iso(-DAY), ...over };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("capability gating", () => {
  it("renders nothing and requests nothing when the server says history is off (guest and registered)", async () => {
    for (const tier of ["guest", "registered"] as const) {
      const store = await seededStore();
      await addHome(store);
      const app = renderApp({ historyEnabled: false, tier, local: store });
      await waitFor(() => expect(app.calls("POST", "/v1/session:bootstrap")).toHaveLength(1));
      await new Promise((r) => setTimeout(r, 30));
      expect(screen.queryByText(HEADING)).toBeNull();
      expect(screen.queryByRole("button", { name: "Show my history and saved places" })).toBeNull();
      expect(app.requests.some((r) => r.url.startsWith("/v1/history") || r.url.startsWith("/v1/saved-places"))).toBe(false);
      // Even a message that matches the ask rule shows no question while the capability is off.
      await sendText(app.user, "pizza near home");
      await screen.findByText("Here is what I found nearby.");
      expect(screen.queryByText(HOME_QUESTION)).toBeNull();
      app.view.unmount();
    }
  });

  it("renders nothing when the capability is on but the client has no Phase 1 wiring", async () => {
    const app = renderApp({ historyEnabled: true, wirePhase1: false });
    await waitFor(() => expect(app.calls("POST", "/v1/session:bootstrap")).toHaveLength(1));
    expect(screen.queryByText(HEADING)).toBeNull();
  });

  it("shows the (collapsed) panel only when the server's capability is true and loads nothing until opened", async () => {
    const app = renderApp({ historyEnabled: true, local: await seededStore() });
    expect(await screen.findByText(HEADING)).toBeInTheDocument();
    expect(screen.queryByText("History")).toBeNull();
    expect(app.requests.some((r) => r.url.startsWith("/v1/history") || r.url.startsWith("/v1/saved-places"))).toBe(false);
  });
});

describe("guest history (on the device)", () => {
  it("lists entries by the user's label and tag, states the retention period, and the picker shortens it", async () => {
    const store = await seededStore();
    await addOuting(store, "new1");
    await addOuting(store, "old1", { label: "Old trip", at: iso(-60 * DAY) });
    const app = renderApp({ historyEnabled: true, local: store });
    await openPanel(app.user);

    expect(await screen.findByText("Date night spot", undefined, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.getByText("Old trip")).toBeInTheDocument();
    expect(screen.getAllByText(/Food and drink/).length).toBe(2);
    expect(screen.getByText(/kept on this device for 90 days/)).toBeInTheDocument();
    expect(screen.getByText(/the default is 90 and the longest is 180/)).toBeInTheDocument();
    const picker = screen.getByLabelText("Keep my history for") as HTMLSelectElement;
    expect([...picker.options].map((o) => o.value)).toEqual(["30", "60", "90", "120", "150", "180"]);

    await app.user.selectOptions(picker, "30");
    await waitFor(() => expect(screen.queryByText("Old trip")).toBeNull());
    expect(screen.getByText("Date night spot")).toBeInTheDocument();
    expect(screen.getByText(/kept on this device for 30 days/)).toBeInTheDocument();
    expect(await store.retentionDays()).toBe(30);
    expect(app.requests.some((r) => r.url.startsWith("/v1/history"))).toBe(false); // guests never use server history
  });

  it("shows the empty state", async () => {
    const app = renderApp({ historyEnabled: true, local: await seededStore() });
    await openPanel(app.user);
    expect(await screen.findByText(/No outings are saved yet/)).toBeInTheDocument();
    expect(screen.getByText(/No saved places yet/)).toBeInTheDocument();
  });

  it("fetches place details by ID only on request, in memory only, and says 'Details unavailable' on a miss", async () => {
    const store = await seededStore();
    await addOuting(store, "a1");
    let miss = false;
    const app = renderApp({
      historyEnabled: true,
      local: store,
      routes: {
        "POST /v1/places/details": () => json({ schema_version: 1, kind: "place_details", places: miss ? [] : [placeCard({ place_id: "ChIJa1" })] }),
      },
    });
    await openPanel(app.user);
    await screen.findByText("Date night spot");
    expect(app.calls("POST", "/v1/places/details")).toHaveLength(0);
    await app.user.click(screen.getByRole("button", { name: "Show place details" }));
    expect(await screen.findByText(/Canary Cafe/)).toBeInTheDocument();
    expect(app.calls("POST", "/v1/places/details")[0]?.body).toMatchObject({ place_ids: ["ChIJa1"] });
    expect(screen.getByText("Google Maps")).toBeInTheDocument();
    // Never stored: no Maps content, address or rating in IndexedDB.
    const idb = JSON.stringify(await store.exportAll());
    expect(idb).not.toMatch(/Canary|Testville|12 Canary|google\.com\/maps/i);

    await app.user.click(screen.getByRole("button", { name: "Hide place details" }));
    expect(screen.queryByText(/Canary Cafe/)).toBeNull();
    miss = true;
    await app.user.click(screen.getByRole("button", { name: "Show place details" }));
    expect(await screen.findByText("Details unavailable")).toBeInTheDocument();
  });

  it("edits a label, deletes one entry, and clears all after a confirm", async () => {
    const store = await seededStore();
    await addOuting(store, "a1");
    await addOuting(store, "a2", { label: "Second spot", at: iso(-3 * DAY) });
    const app = renderApp({ historyEnabled: true, local: store });
    await openPanel(app.user);
    await screen.findByText("Date night spot");

    const first = screen.getByText("Date night spot").closest("li")!;
    await app.user.click(within(first).getByRole("button", { name: "Edit label" }));
    const input = within(first).getByLabelText("Your label");
    await app.user.clear(input);
    await app.user.type(input, "Renamed spot");
    await app.user.click(within(first).getByRole("button", { name: "Save label" }));
    expect(await screen.findByText("Renamed spot")).toBeInTheDocument();
    expect((await store.get("outing_log", "a1"))?.label).toBe("Renamed spot");

    const second = screen.getByText("Second spot").closest("li")!;
    await app.user.click(within(second).getByRole("button", { name: "Delete entry" }));
    await app.user.click(within(second).getByRole("button", { name: "Yes, delete this entry" }));
    await waitFor(() => expect(screen.queryByText("Second spot")).toBeNull());
    expect(await store.get("outing_log", "a2")).toBeUndefined();

    await app.user.click(screen.getByRole("button", { name: "Clear all history" }));
    expect(await store.getAll("outing_log")).toHaveLength(1); // nothing until confirmed
    await app.user.click(screen.getByRole("button", { name: "Yes, clear all" }));
    await waitFor(async () => expect(await store.getAll("outing_log")).toEqual([]));
    expect(await screen.findByText(/No outings are saved yet/)).toBeInTheDocument();
  });

  it("exports the history as a file built on the client", async () => {
    const store = await seededStore();
    await addOuting(store, "a1");
    const blobs: Blob[] = [];
    vi.spyOn(URL, "createObjectURL").mockImplementation((b) => (blobs.push(b as Blob), "blob:h"));
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    const app = renderApp({ historyEnabled: true, local: store });
    await openPanel(app.user);
    await screen.findByText("Date night spot");
    await app.user.click(screen.getByRole("button", { name: "Export my history" }));
    expect(await screen.findByText("Exported.")).toBeInTheDocument();
    const text = await blobs[0]!.text();
    expect(text).toContain("ChIJa1");
    expect(text).toContain("Date night spot");
  });

  it("explains a refused write when the on-device notice was declined", async () => {
    const store = await seededStore();
    await addOuting(store, "a1");
    await store.setNotice(false);
    const app = renderApp({ historyEnabled: true, local: store });
    await openPanel(app.user);
    const row = (await screen.findByText("Date night spot")).closest("li")!;
    await app.user.click(within(row).getByRole("button", { name: "Edit label" }));
    await app.user.type(within(row).getByLabelText("Your label"), "x");
    await app.user.click(within(row).getByRole("button", { name: "Save label" }));
    expect(await screen.findByText(/needs your OK first/)).toBeInTheDocument();
  });

  it("shows a storage error state when the list cannot be read", async () => {
    const store = await seededStore();
    vi.spyOn(store, "getAll").mockRejectedValue(new Error("boom"));
    const app = renderApp({ historyEnabled: true, local: store });
    await openPanel(app.user);
    expect(await screen.findAllByText(/Could not read or change what is saved on this device/)).not.toHaveLength(0);
  });
});

describe("registered history (on the server) with consent", () => {
  it("asks for HISTORY_STORE first (placeholder notice), then lists the server records; guests' device store is untouched", async () => {
    const store = await seededStore();
    const app = renderApp({
      historyEnabled: true,
      tier: "registered",
      local: store,
      routes: {
        "POST /v1/me/consents": json(CONSENT("HISTORY_STORE", "history-notice-1")),
        "GET /v1/history/records": json({ schema_version: 1, kind: "history_record_list", records: [serverOuting()] }),
        "GET /v1/saved-places": json({ schema_version: 1, kind: "saved_place_list", places: [] }),
      },
    });
    await openPanel(app.user);
    expect(await screen.findByText(/PLACEHOLDER NOTICE \(history-notice-1\), pending counsel review \(issue #27\)/)).toBeInTheDocument();
    expect(app.calls("GET", "/v1/history/records")).toHaveLength(0); // nothing is read before consent
    expect(screen.getAllByText(/kept for up to 180 days/).length).toBeGreaterThan(0);
    expect(screen.queryByLabelText("Keep my history for")).toBeNull();

    await app.user.click(screen.getByRole("button", { name: "Agree and save my history" }));
    expect(await screen.findByText("Server dinner")).toBeInTheDocument();
    expect(app.calls("POST", "/v1/me/consents").at(-1)?.body).toEqual({ purpose: "HISTORY_STORE", notice_version: "history-notice-1" });
    expect(screen.getByText(/Went \(you confirmed\)/)).toBeInTheDocument();
    // Editing or removing ONE record has no endpoint yet, so those controls are not offered.
    expect(screen.queryByRole("button", { name: "Edit label" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete entry" })).toBeNull();
    expect(screen.getByText(/not available yet for synced history/)).toBeInTheDocument();
    expect(await store.getAll("outing_log")).toEqual([]);
  });

  it("clears all on the server after a confirm, and turning history off withdraws the consent", async () => {
    let records = [serverOuting()];
    const app = renderApp({
      historyEnabled: true,
      tier: "registered",
      local: await seededStore(),
      routes: {
        "GET /v1/me/consents": json({ consents: [CONSENT("HISTORY_STORE")] }),
        "GET /v1/history/records": () => json({ schema_version: 1, kind: "history_record_list", records }),
        "DELETE /v1/history/records": () => {
          records = [];
          return json({ schema_version: 1, kind: "history_delete_result", deleted: 1 });
        },
        "DELETE /v1/me/consents/HISTORY_STORE": json({}),
        "GET /v1/saved-places": json({ schema_version: 1, kind: "saved_place_list", places: [] }),
      },
    });
    await openPanel(app.user);
    await screen.findByText("Server dinner");
    await app.user.click(screen.getByRole("button", { name: "Clear all history" }));
    expect(app.calls("DELETE", "/v1/history/records")).toHaveLength(0);
    await app.user.click(screen.getByRole("button", { name: "Yes, clear all" }));
    expect(await screen.findByText(/No outings are saved yet/)).toBeInTheDocument();
    await app.user.click(screen.getByRole("button", { name: "Stop saving my history" }));
    await waitFor(() => expect(app.calls("DELETE", "/v1/me/consents/HISTORY_STORE")).toHaveLength(1));
    expect(await screen.findByRole("button", { name: "Agree and save my history" })).toBeInTheDocument();
  });

  it("shows 'not available yet' when the server answers DISABLED, and a provider error with fixed copy", async () => {
    const off = renderApp({
      historyEnabled: true,
      tier: "registered",
      routes: {
        "GET /v1/me/consents": json({ consents: [CONSENT("HISTORY_STORE")] }),
        "GET /v1/history/records": json(errorBody("NOT_FOUND", "DISABLED", "raw secret"), 404),
        "GET /v1/saved-places": json(errorBody("NOT_FOUND", "DISABLED"), 404),
      },
    });
    await openPanel(off.user);
    expect((await screen.findAllByText(/not available yet/)).length).toBeGreaterThan(0);
    expect(screen.queryByText(/raw secret/)).toBeNull();
  });
});

describe("saved places", () => {
  it("lists, renames and deletes a guest's saved place on the device", async () => {
    const store = await seededStore();
    await addHome(store);
    const app = renderApp({ historyEnabled: true, local: store });
    await openPanel(app.user);
    const row = (await screen.findByText("Home", { selector: ".place-name" })).closest("li")!;
    await app.user.click(within(row).getByRole("button", { name: "Rename" }));
    const input = within(row).getByLabelText("Name");
    await app.user.clear(input);
    await app.user.type(input, "My flat");
    await app.user.click(within(row).getByRole("button", { name: "Save name" }));
    expect(await screen.findByText("My flat", { selector: ".place-name" })).toBeInTheDocument();
    expect((await store.get("saved_places", "home"))?.label).toBe("My flat");
    await app.user.click(screen.getByRole("button", { name: "Delete" }));
    await app.user.click(screen.getByRole("button", { name: "Yes, delete My flat" }));
    await waitFor(async () => expect(await store.getAll("saved_places")).toEqual([]));
  });

  it("registered: lists from the server, renames with the same position, deletes by place ID", async () => {
    let places = [serverSaved()];
    const app = renderApp({
      historyEnabled: true,
      tier: "registered",
      routes: {
        "GET /v1/me/consents": json({ consents: [CONSENT("HISTORY_STORE")] }),
        "GET /v1/history/records": json({ schema_version: 1, kind: "history_record_list", records: [] }),
        "GET /v1/saved-places": () => json({ schema_version: 1, kind: "saved_place_list", places }),
        "PUT /v1/saved-places": (r) => {
          places = [serverSaved({ user_label: (r.body as { user_label: string }).user_label })];
          return json(places[0]);
        },
        "DELETE /v1/saved-places": () => {
          places = [];
          return json({ schema_version: 1, kind: "saved_place_delete_result", deleted: 1 });
        },
      },
    });
    await openPanel(app.user);
    const row = (await screen.findByText("Home", { selector: ".place-name" })).closest("li")!;
    await app.user.click(within(row).getByRole("button", { name: "Rename" }));
    const input = within(row).getByLabelText("Name");
    await app.user.clear(input);
    await app.user.type(input, "Flat");
    await app.user.click(within(row).getByRole("button", { name: "Save name" }));
    expect(await screen.findByText("Flat", { selector: ".place-name" })).toBeInTheDocument();
    expect(app.calls("PUT", "/v1/saved-places")[0]?.body).toEqual({
      schema_version: 1,
      place_id: "here-srv",
      label_kind: "HOME",
      user_label: "Flat",
      position: { latitude: 48.858, longitude: 2.294 },
    });
    await app.user.click(screen.getByRole("button", { name: "Delete" }));
    await app.user.click(screen.getByRole("button", { name: "Yes, delete Flat" }));
    await waitFor(() => expect(app.calls("DELETE", "/v1/saved-places")).toHaveLength(1));
    expect(app.calls("DELETE", "/v1/saved-places")[0]?.url).toContain("place_id=here-srv");
    expect(await screen.findByText(/No saved places yet/)).toBeInTheDocument();
  });
});

describe("saving Home goes through the confirmed SAVE_PLACE action", () => {
  const proposeRoutes = (extra: Record<string, Handler | Response> = {}): Record<string, Handler | Response> => ({
    "POST /v1/me/consents": json(CONSENT("LOCATION_FOREGROUND", "location-notice-1")),
    "POST /v1/actions/propose": () => json(pendingAction({ id: "act-save", effect: "SAVE_PLACE", target: "here-whatever", preview: "Save this place as Home on this device.", idempotency_key: "key-act-save" })),
    "POST /v1/actions/act-save/confirm": json({ schema_version: 1, kind: "confirm_action_response", action: pendingAction({ id: "act-save", effect: "SAVE_PLACE", confirmed_at: iso(0) }) }),
    ...extra,
  });

  it("guest: the fixed question appears from the local keyword rule; Yes proposes, Confirm saves a rounded position on the device", async () => {
    const geo = mockGeolocation("ok");
    const store = await seededStore();
    const app = renderApp({ historyEnabled: true, local: store, routes: proposeRoutes() });
    await turnOnLocation(app.user);
    await sendText(app.user, "pizza near home");
    expect(await screen.findByText(HOME_QUESTION)).toBeInTheDocument();
    expect(app.calls("POST", "/v1/actions/propose")).toHaveLength(0); // asking proposes nothing

    await app.user.click(screen.getByRole("button", { name: "Yes, save as Home" }));
    expect(await screen.findByText("Save this place as Home on this device.")).toBeInTheDocument();
    const [propose] = app.calls("POST", "/v1/actions/propose");
    expect(propose?.body).toMatchObject({ effect: "SAVE_PLACE", label_kind: "HOME", user_label: "Home", workspace_id: "ws-1" });
    expect((propose?.body as { target: string }).target).toMatch(/^here-[0-9a-f]+$/);
    expect(JSON.stringify(propose?.body)).not.toMatch(/48\.85|2\.29|latitude|longitude/);
    expect(propose?.headers.get("idempotency-key")).toMatch(/^web-/);
    expect(await store.getAll("saved_places")).toEqual([]); // preview only: nothing saved yet

    await app.user.click(screen.getByRole("button", { name: "Confirm" }));
    expect(await screen.findByText(/Home is saved on this device/)).toBeInTheDocument();
    expect(app.calls("POST", "/v1/actions/act-save/confirm")[0]?.headers.get("idempotency-key")).toBe("key-act-save");
    const [home] = await store.getAll("saved_places");
    expect(home).toMatchObject({ id: "home", kind: "HOME", label: "Home", position: { lat: round3(48.8584), lng: round3(2.2945) } });

    // Canary: the unrounded position is nowhere in storage, requests or URLs.
    const idb = JSON.stringify(await store.exportAll());
    expect(idb).not.toMatch(/48\.8584|2\.2945/);
    for (const r of app.requests) expect(r.url).not.toMatch(/48\.85|2\.29/);
    expect(geo.watchPosition).not.toHaveBeenCalled();
  });

  it("Cancel saves nothing", async () => {
    mockGeolocation("ok");
    const store = await seededStore();
    const app = renderApp({
      historyEnabled: true,
      local: store,
      routes: proposeRoutes({ "POST /v1/actions/act-save/cancel": json({ schema_version: 1, kind: "cancel_action_response", action_id: "act-save", cancelled: true }) }),
    });
    await turnOnLocation(app.user);
    await sendText(app.user, "lunch near home");
    await app.user.click(await screen.findByRole("button", { name: "Yes, save as Home" }));
    await screen.findByText("Save this place as Home on this device.");
    await app.user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(await screen.findByText(/Cancelled\. Nothing was done/)).toBeInTheDocument();
    expect(await store.getAll("saved_places")).toEqual([]);
  });

  it("needs the location consent first and does not save without it", async () => {
    mockGeolocation("ok");
    const store = await seededStore();
    const app = renderApp({ historyEnabled: true, local: store, routes: proposeRoutes() });
    await sendText(app.user, "lunch near home");
    await app.user.click(await screen.findByRole("button", { name: "Yes, save as Home" }));
    expect(await screen.findByText(/Turn on location sharing above/)).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Agree and use my location" })).toBeInTheDocument(); // the existing consent flow
    expect(app.calls("POST", "/v1/actions/propose")).toHaveLength(0);
  });

  it("shows a geolocation problem in fixed copy", async () => {
    mockGeolocation("ok");
    const store = await seededStore();
    const app = renderApp({ historyEnabled: true, local: store, routes: proposeRoutes() });
    await turnOnLocation(app.user);
    mockGeolocation("denied");
    await sendText(app.user, "lunch near home");
    await app.user.click(await screen.findByRole("button", { name: "Yes, save as Home" }));
    expect((await screen.findAllByText(/Your browser blocked location access/)).length).toBeGreaterThan(0);
  });

  it("guest without the on-device notice is asked to accept it before anything is proposed", async () => {
    mockGeolocation("ok");
    const store = freshStore(); // notice unset
    const app = renderApp({ historyEnabled: true, local: store, routes: proposeRoutes() });
    await turnOnLocation(app.user);
    await sendText(app.user, "lunch near home");
    await app.user.click(await screen.findByRole("button", { name: "Yes, save as Home" }));
    const dialog = await screen.findByRole("dialog", { name: "Saved on this device" });
    expect(app.calls("POST", "/v1/actions/propose")).toHaveLength(0);
    await app.user.click(within(dialog).getByRole("button", { name: "OK, save on this device" }));
    expect(await screen.findByText("Save this place as Home on this device.")).toBeInTheDocument();
  });

  it("registered: asks for SAVED_PLACE consent (placeholder), proposes, confirms, then finishes the save on the server with the rounded position", async () => {
    mockGeolocation("ok");
    const app = renderApp({
      historyEnabled: true,
      tier: "registered",
      local: await seededStore(),
      routes: proposeRoutes({
        "POST /v1/me/consents": (r) => json(CONSENT((r.body as { purpose: string }).purpose, (r.body as { notice_version: string }).notice_version)),
        "GET /v1/history/records": json({ schema_version: 1, kind: "history_record_list", records: [] }),
        "GET /v1/saved-places": json({ schema_version: 1, kind: "saved_place_list", places: [] }),
        "PUT /v1/saved-places": (r) => json(serverSaved({ place_id: (r.body as { place_id: string }).place_id })),
      }),
    });
    await turnOnLocation(app.user);
    await openPanel(app.user);
    await app.user.click(await screen.findByRole("button", { name: "Agree and save my history" }));
    await screen.findByText(/No outings are saved yet/);
    await app.user.click(screen.getByRole("button", { name: "Save where I am now as Home" }));
    expect(await screen.findByText(/PLACEHOLDER NOTICE \(saved-place-notice-1\), pending counsel review \(issue #27\)/)).toBeInTheDocument();
    expect(app.calls("POST", "/v1/actions/propose")).toHaveLength(0);
    await app.user.click(screen.getByRole("button", { name: "Agree and continue" }));
    expect(app.calls("POST", "/v1/me/consents").at(-1)?.body).toEqual({ purpose: "SAVED_PLACE", notice_version: "saved-place-notice-1" });
    await screen.findByText("Save this place as Home on this device.");
    await app.user.click(screen.getByRole("button", { name: "Confirm" }));
    expect(await screen.findByText(/Home is saved to your account/)).toBeInTheDocument();
    const put = app.calls("PUT", "/v1/saved-places")[0]?.body as { position: unknown; label_kind: string };
    expect(put.label_kind).toBe("HOME");
    expect(put.position).toEqual({ latitude: round3(48.8584), longitude: round3(2.2945) });
    expect(app.requests.every((r) => !/48\.85|2\.29/.test(r.url))).toBe(true);
  });

  it("registered users are never asked on a client-side keyword (the server decides)", async () => {
    const app = renderApp({
      historyEnabled: true,
      tier: "registered",
      local: await seededStore(),
      routes: { "POST /v1/agent/turns": json(turnResponse()) },
    });
    await screen.findByText(HEADING);
    await sendText(app.user, "pizza near home");
    await screen.findByText("Here is what I found nearby.");
    expect(screen.queryByText(HOME_QUESTION)).toBeNull();
    expect(askSignalFromTurn(turnResponse())).toBeNull();
  });
});

describe("ask policy", () => {
  it("detects near-home and near-work phrases deterministically from the user's own text", () => {
    for (const t of ["pizza near home", "Close to my home please", "somewhere by my house", "cafe around home"]) expect(detectNearKind(t)).toBe("HOME");
    for (const t of ["lunch near work", "near the office", "closest to my workplace"]) expect(detectNearKind(t)).toBe("WORK");
    for (const t of ["a home improvement store", "homework help spots", "dinner downtown", "nearby cafe", "work out spots"]) expect(detectNearKind(t)).toBeNull();
    expect(HOME_QUESTION).toBe("Is where you are now your home? Save it as Home?");
    expect(WORK_QUESTION).toBe("Is where you are now your work? Save it as Work?");
  });

  it("asks only without a saved label, outside the 30-day decline cooldown and without don't-ask-again", () => {
    const now = Date.now();
    const none = { dontAskAgain: false, declinedAt: null };
    expect(shouldAsk(none, false, now)).toBe(true);
    expect(shouldAsk(none, true, now)).toBe(false);
    expect(shouldAsk({ ...none, dontAskAgain: true }, false, now)).toBe(false);
    expect(shouldAsk({ ...none, declinedAt: new Date(now - 29 * DAY).toISOString() }, false, now)).toBe(false);
    expect(shouldAsk({ ...none, declinedAt: new Date(now - (DECLINE_COOLDOWN_DAYS + 1) * DAY).toISOString() }, false, now)).toBe(true);
    expect(cooldownActive({ dontAskAgain: false, declinedAt: "garbage" }, now)).toBe(true); // unreadable is treated as cooling down
  });

  it("'Not now' starts a 30-day cooldown that survives a reload; after 30 days the question may return", async () => {
    const store = await seededStore();
    const first = renderApp({ historyEnabled: true, local: store });
    await sendText(first.user, "dinner near home");
    await first.user.click(await screen.findByRole("button", { name: "Not now" }));
    expect(screen.queryByText(HOME_QUESTION)).toBeNull();
    await waitFor(async () => expect((await store.get("ask_state", "HOME"))?.last_asked_at).not.toBeNull());
    expect((await store.get("ask_state", "HOME"))?.dont_ask_again).toBe(false);
    first.view.unmount();

    const second = renderApp({ historyEnabled: true, local: store });
    await sendText(second.user, "lunch near home");
    await screen.findAllByText("Here is what I found nearby.");
    await new Promise((r) => setTimeout(r, 40));
    expect(screen.queryByText(HOME_QUESTION)).toBeNull();
    second.view.unmount();

    await store.put("ask_state", { schema_version: SCHEMA_VERSION, key: "HOME", dont_ask_again: false, last_asked_at: iso(-31 * DAY) });
    const third = renderApp({ historyEnabled: true, local: store });
    await sendText(third.user, "brunch near home");
    expect(await screen.findByText(HOME_QUESTION)).toBeInTheDocument();
  });

  it("'Don't ask again' is stored locally and holds even long after", async () => {
    const store = await seededStore();
    const first = renderApp({ historyEnabled: true, local: store });
    await sendText(first.user, "dinner near work");
    expect(await screen.findByText(WORK_QUESTION)).toBeInTheDocument();
    await first.user.click(screen.getByRole("button", { name: "Don't ask again" }));
    await waitFor(async () => expect((await store.get("ask_state", "WORK"))?.dont_ask_again).toBe(true));
    // keyed by the label, never a coordinate
    expect((await store.getAll("ask_state")).map((r) => r.key)).toEqual(["WORK"]);
    first.view.unmount();

    await store.put("ask_state", { schema_version: SCHEMA_VERSION, key: "WORK", dont_ask_again: true, last_asked_at: iso(-200 * DAY) });
    const second = renderApp({ historyEnabled: true, local: store });
    await sendText(second.user, "lunch near the office");
    await screen.findAllByText("Here is what I found nearby.");
    await new Promise((r) => setTimeout(r, 40));
    expect(screen.queryByText(WORK_QUESTION)).toBeNull();
  });

  it("does not ask when that label is already saved", async () => {
    const store = await seededStore();
    await addHome(store);
    const app = renderApp({ historyEnabled: true, local: store });
    await sendText(app.user, "dinner near home");
    await screen.findByText("Here is what I found nearby.");
    await new Promise((r) => setTimeout(r, 40));
    expect(screen.queryByText(HOME_QUESTION)).toBeNull();
  });

  it("still honours a decline for this visit when nothing can be saved on the device", async () => {
    const store = freshStore(); // notice unset: ask_state writes are refused
    const app = renderApp({ historyEnabled: true, local: store });
    await sendText(app.user, "dinner near home");
    await app.user.click(await screen.findByRole("button", { name: "Not now" }));
    await sendText(app.user, "again near home");
    await new Promise((r) => setTimeout(r, 40));
    expect(screen.queryByText(HOME_QUESTION)).toBeNull();
  });
});

describe("a saved Home is never silently applied", () => {
  it("sends the rounded saved position only after the explicit tap, once, through the consented location path", async () => {
    const geo = mockGeolocation("ok");
    const store = await seededStore();
    await addHome(store);
    const app = renderApp({
      historyEnabled: true,
      local: store,
      routes: { "POST /v1/me/consents": json(CONSENT("LOCATION_FOREGROUND", "location-notice-1")) },
    });
    await turnOnLocation(app.user);
    // 1. No tap: the send uses the device position as before, never the saved Home.
    await sendText(app.user, "coffee");
    await screen.findByText("Here is what I found nearby.");
    expect(app.turns()[0]?.body).toMatchObject({ location: { latitude: 48.8584, longitude: 2.2945 } });

    // 2. Explicit tap: the chip shows, and the next send carries the rounded saved position.
    await openPanel(app.user);
    await app.user.click(await screen.findByRole("button", { name: "Use my saved Home as the starting area" }));
    expect(screen.getByText(/Starting area for your next message/)).toBeInTheDocument();
    const reads = geo.getCurrentPosition.mock.calls.length;
    await sendText(app.user, "tea");
    await waitFor(() => expect(app.turns()).toHaveLength(2));
    expect(app.turns()[1]?.body).toMatchObject({ location: { latitude: 48.858, longitude: 2.294 } });
    expect(geo.getCurrentPosition.mock.calls.length).toBe(reads); // the saved position replaced the read
    expect(screen.queryByText(/Starting area for your next message/)).toBeNull();

    // 3. One-shot: the message after that is back to the device position.
    await screen.findAllByText("Here is what I found nearby.");
    await sendText(app.user, "snack");
    await waitFor(() => expect(app.turns()).toHaveLength(3));
    expect(app.turns()[2]?.body).toMatchObject({ location: { latitude: 48.8584 } });
    for (const r of app.requests) expect(r.url).not.toMatch(/48\.85|2\.29/);
  });

  it("can be undone before sending", async () => {
    mockGeolocation("ok");
    const store = await seededStore();
    await addHome(store);
    const app = renderApp({
      historyEnabled: true,
      local: store,
      routes: { "POST /v1/me/consents": json(CONSENT("LOCATION_FOREGROUND", "location-notice-1")) },
    });
    await turnOnLocation(app.user);
    await openPanel(app.user);
    await app.user.click(await screen.findByRole("button", { name: "Use my saved Home as the starting area" }));
    await app.user.click(screen.getByRole("button", { name: "Don't use it" }));
    await sendText(app.user, "coffee");
    await screen.findByText("Here is what I found nearby.");
    expect(app.turns()[0]?.body).toMatchObject({ location: { latitude: 48.8584 } });
  });

  it("needs the location consent: the tap opens the existing consent flow and arms nothing", async () => {
    mockGeolocation("ok");
    const store = await seededStore();
    await addHome(store);
    const app = renderApp({ historyEnabled: true, local: store });
    await openPanel(app.user);
    await app.user.click(await screen.findByRole("button", { name: "Use my saved Home as the starting area" }));
    expect(await screen.findByRole("button", { name: "Agree and use my location" })).toBeInTheDocument();
    expect(screen.queryByText(/Starting area for your next message/)).toBeNull();
    expect(screen.getByText(/Turn on location sharing above/)).toBeInTheDocument();
  });
});

describe("canary: nothing sensitive reaches storage or the screen's persisted state", () => {
  it("no Maps content, unrounded coordinates, tokens or labels-as-keys in IndexedDB or web storage after the Phase 1 flows", async () => {
    mockGeolocation("ok");
    const store = await seededStore();
    await addOuting(store, "a1");
    const app = renderApp({
      historyEnabled: true,
      local: store,
      routes: {
        "POST /v1/me/consents": json(CONSENT("LOCATION_FOREGROUND", "location-notice-1")),
        "POST /v1/places/details": json({ schema_version: 1, kind: "place_details", places: [placeCard({ place_id: "ChIJa1" })] }),
        "POST /v1/actions/propose": json(pendingAction({ id: "act-save", effect: "SAVE_PLACE", target: "here-z", preview: "Save Home." })),
        "POST /v1/actions/act-save/confirm": json({ schema_version: 1, kind: "confirm_action_response", action: pendingAction({ id: "act-save", effect: "SAVE_PLACE" }) }),
      },
    });
    await turnOnLocation(app.user);
    await openPanel(app.user);
    await app.user.click(await screen.findByRole("button", { name: "Show place details" }));
    await screen.findByText(/Canary Cafe/);
    await app.user.click(screen.getByRole("button", { name: "Save where I am now as Home" }));
    await screen.findByText("Save Home.");
    await app.user.click(screen.getByRole("button", { name: "Confirm" }));
    await screen.findByText(/Home is saved on this device/);

    const dump = JSON.stringify(await store.exportAll()) + JSON.stringify({ ...localStorage, ...sessionStorage });
    for (const canary of ["Canary", "Testville", "google.com/maps", "48.8584", "2.2945", "dev-only-public-token", "nonce-secret"]) {
      expect(dump, canary).not.toContain(canary);
    }
    for (const r of app.requests) expect(r.url).not.toMatch(/48\.85|2\.29|Canary/);
  });
});
