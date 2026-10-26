import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Chat } from "../components/Chat";
import { LOCAL_NOTICE_TEXT } from "../storage";
import { freshStore } from "./idb";
import { errorBody, json, mockGeolocation } from "./support";
import { boardBackend, MAPS_URL, pendingAction, placeCard, PLACE_A, renderWithDiscovery, routeCheck, turnWithIds, workspace } from "./boardSupport";

afterEach(() => vi.restoreAllMocks());

const CONSENT_ON = json({ consents: [{ id: "c1", purpose: "LOCATION_FOREGROUND", notice_version: "location-notice-1", processors: ["GOOGLE"], granted_at: "2026-10-17T12:00:00Z" }] });
const iso = (offsetMs: number) => new Date(Date.now() + offsetMs).toISOString();

async function seedReminder(store: ReturnType<typeof freshStore>, dueInMs: number, id = "r1") {
  await store.setNotice(true);
  await store.put("reminders", { schema_version: 1, id, place_id: PLACE_A, label: "Dinner spot", due_at: iso(dueInMs), created_at: iso(-3600_000) });
}

describe("reminder banner", () => {
  it("shows a due reminder on open, and snooze and dismiss work locally", async () => {
    const store = freshStore();
    await seedReminder(store, -60_000);
    const backend = boardBackend();
    render(<Chat api={backend.api} boardApi={backend.boardApi} local={store} />);
    const banner = within(await screen.findByLabelText("Reminders"));
    expect(banner.getByText('Time to head to "Dinner spot".')).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(banner.getByRole("button", { name: "Snooze 10 minutes" }));
    // no longer due: it moves to "coming up"
    expect(await banner.findByText(/Coming up at .*: "Dinner spot"\./)).toBeInTheDocument();
    expect(banner.queryByText('Time to head to "Dinner spot".')).toBeNull();
    const [snoozed] = await store.getAll("reminders");
    expect(Date.parse(snoozed!.due_at)).toBeGreaterThan(Date.now() + 8 * 60_000);
    expect(snoozed!.label).toBe("Dinner spot");
  });

  it("shows an upcoming reminder and dismisses it", async () => {
    const store = freshStore();
    await seedReminder(store, 20 * 60_000);
    const backend = boardBackend();
    render(<Chat api={backend.api} boardApi={backend.boardApi} local={store} />);
    const banner = within(await screen.findByLabelText("Reminders"));
    expect(banner.getByText(/Coming up at .*: "Dinner spot"\./)).toBeInTheDocument();
    await userEvent.click(banner.getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(screen.queryByLabelText("Reminders")).toBeNull());
    expect(await store.getAll("reminders")).toEqual([]);
  });

  it("shows nothing for a far-future reminder or without a store", async () => {
    const store = freshStore();
    await seedReminder(store, 5 * 3600_000);
    const backend = boardBackend();
    render(<Chat api={backend.api} boardApi={backend.boardApi} local={store} />);
    await screen.findByText("Trip Board");
    expect(screen.queryByLabelText("Reminders")).toBeNull();
  });

  it("'Did you go?' from a due reminder opens the check-in and clears the reminder", async () => {
    const store = freshStore();
    await seedReminder(store, -1000);
    const backend = boardBackend();
    render(<Chat api={backend.api} boardApi={backend.boardApi} local={store} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Did you go?" }));
    expect(await screen.findByLabelText("Did you go?", { selector: "section" })).toBeInTheDocument();
    expect(await store.getAll("reminders")).toEqual([]);
  });
});

describe("outcome check-in", () => {
  async function openCheckIn(routes: Parameters<typeof boardBackend>[0]) {
    const store = freshStore();
    await seedReminder(store, -1000);
    const backend = boardBackend(routes);
    render(<Chat api={backend.api} boardApi={backend.boardApi} local={store} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Did you go?" }));
    const checkin = within(await screen.findByLabelText("Did you go?", { selector: "section" }));
    return { store, backend, user, checkin };
  }

  it("posts the outcome with a key, then logs locally (place ID and user label only) when the server did not keep it", async () => {
    const log = vi.spyOn(console, "log");
    const { store, backend, user, checkin } = await openCheckIn({
      "POST /v1/outcomes": json({ schema_version: 1, kind: "outcome_ack", recorded: false }),
    });
    await user.click(checkin.getByRole("button", { name: "I went" }));
    await user.type(checkin.getByPlaceholderText("Optional note"), "SECRETNOTE lovely");
    await user.click(checkin.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(screen.queryByLabelText("Did you go?", { selector: "section" })).toBeNull());
    const [post] = backend.calls("POST", "/v1/outcomes");
    expect(post!.body).toMatchObject({ place_id: PLACE_A, outcome: "WENT", note: "SECRETNOTE lovely" });
    expect(post!.headers.get("idempotency-key")).toBeTruthy();
    const logged = await store.getAll("outing_log");
    expect(logged).toHaveLength(1);
    expect(logged[0]).toMatchObject({ place_id: PLACE_A, label: "Dinner spot", outcome: "WENT" });
    expect(JSON.stringify(await store.exportAll())).not.toContain("SECRETNOTE");
    expect(log).not.toHaveBeenCalled();
  });

  it("does not duplicate the report locally when the server recorded it", async () => {
    const { store, user, checkin } = await openCheckIn({
      "POST /v1/outcomes": json({ schema_version: 1, kind: "outcome_ack", recorded: true }),
    });
    await user.click(checkin.getByRole("button", { name: "I did not go" }));
    await user.click(checkin.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(screen.queryByLabelText("Did you go?", { selector: "section" })).toBeNull());
    expect(await store.getAll("outing_log")).toEqual([]);
  });

  it("keeps it on this device when the outcome route is not available yet (typed 501)", async () => {
    const { store, user, checkin } = await openCheckIn({});
    await user.click(checkin.getByRole("button", { name: "I changed plans" }));
    await user.click(checkin.getByRole("button", { name: "Send" }));
    await waitFor(async () => expect(await store.getAll("outing_log")).toHaveLength(1));
    expect((await store.getAll("outing_log"))[0]!.outcome).toBe("CHANGED_PLAN");
  });

  it("retries a failed report with the same key and shows fixed copy", async () => {
    let n = 0;
    const { backend, user, checkin } = await openCheckIn({
      "POST /v1/outcomes": () => (++n === 1 ? json(errorBody("PROVIDER", "PROVIDER_UNAVAILABLE", "RAW"), 503) : json({ schema_version: 1, kind: "outcome_ack", recorded: true })),
    });
    await user.click(checkin.getByRole("button", { name: "I went" }));
    await user.click(checkin.getByRole("button", { name: "Send" }));
    expect(await checkin.findByText("Something went wrong on our side. You can try again.")).toBeInTheDocument();
    expect(screen.queryByText("RAW")).toBeNull();
    await user.click(checkin.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.queryByLabelText("Did you go?", { selector: "section" })).toBeNull());
    const posts = backend.calls("POST", "/v1/outcomes");
    expect(posts[1]!.headers.get("idempotency-key")).toBe(posts[0]!.headers.get("idempotency-key"));
  });

  it("Skip sends nothing", async () => {
    const { backend, user, checkin, store } = await openCheckIn({});
    await user.click(checkin.getByRole("button", { name: "Skip" }));
    expect(backend.calls("POST", "/v1/outcomes")).toHaveLength(0);
    expect(await store.getAll("outing_log")).toEqual([]);
  });
});

describe("reminder through the pending action protocol", () => {
  it("asks for the first-save notice, then saves place ID + user label + due time and shows the banner", async () => {
    const store = freshStore(); // notice unset
    const backend = boardBackend({
      "GET /v1/workspace": json(workspace()),
      "POST /v1/agent/turns": turnWithIds(),
      "POST /v1/places/details": json({ schema_version: 1, kind: "place_details", places: [placeCard()] }),
      "POST /v1/actions/propose": json(pendingAction({ id: "act-2", effect: "SET_REMINDER", preview: "Remind you about this place.", idempotency_key: "key-act-2" })),
      "POST /v1/actions/act-2/confirm": json({ schema_version: 1, kind: "confirm_action_response", action: pendingAction({ id: "act-2", effect: "SET_REMINDER" }) }),
    });
    const user = await renderWithDiscovery(backend, store, false);
    await user.click(screen.getAllByRole("button", { name: /^Check this place/ })[0]!);
    await screen.findByText("Canary Cafe <b>Name</b>");
    await user.click(screen.getByRole("button", { name: "Remind me" }));
    const card = within(await screen.findByLabelText("Set a reminder", { selector: "section" }));
    expect(card.getByText("Remind you about this place.")).toBeInTheDocument();
    await user.selectOptions(card.getByRole("combobox"), "30");
    await user.click(card.getByRole("button", { name: "Confirm" }));
    expect(await card.findAllByText(LOCAL_NOTICE_TEXT)).not.toHaveLength(0);
    expect(await store.getAll("reminders")).toEqual([]); // nothing saved before the notice
    await user.click(card.getByRole("button", { name: "OK, save on this device" }));
    await waitFor(async () => expect(await store.getAll("reminders")).toHaveLength(1));
    const [saved] = await store.getAll("reminders");
    expect(saved).toMatchObject({ place_id: PLACE_A, label: "My pick 1" });
    expect(Date.parse(saved!.due_at) - Date.now()).toBeGreaterThan(25 * 60_000);
    expect(Date.parse(saved!.due_at) - Date.now()).toBeLessThan(31 * 60_000);
    expect(backend.calls("POST", "/v1/actions/act-2/confirm")[0]!.headers.get("idempotency-key")).toBe("key-act-2");
    expect(await screen.findByLabelText("Reminders")).toBeInTheDocument();
    expect(screen.getByText(/A closed browser tab cannot fire a reminder/)).toBeInTheDocument();
  });

  it("sends a key and a due time, re-proposes on a new time, and saves the server's typed reminder", async () => {
    const store = freshStore();
    const serverDue = iso(45 * 60_000);
    let proposals = 0;
    const backend = boardBackend({
      "GET /v1/workspace": json(workspace({ latest_plan: undefined, plan_versions: [] })),
      "POST /v1/agent/turns": turnWithIds(),
      "POST /v1/places/details": json({ schema_version: 1, kind: "place_details", places: [placeCard()] }),
      "POST /v1/actions/propose": () =>
        json(pendingAction({ id: `act-${++proposals}`, effect: "SET_REMINDER", preview: "Remind you.", idempotency_key: `key-prop-${proposals}` })),
      "POST /v1/actions/act-2/confirm": json({
        schema_version: 1,
        kind: "confirm_action_response",
        action: pendingAction({ id: "act-2", effect: "SET_REMINDER" }),
        reminder: { schema_version: 1, kind: "reminder_item", action_id: "act-2", place_id: PLACE_A, user_label: "My pick 1", due_at: serverDue },
      }),
      "POST /v1/actions/act-1/cancel": json({ schema_version: 1, kind: "cancel_action_response", action_id: "act-1", cancelled: true }),
    });
    const user = await renderWithDiscovery(backend, store); // accepts the first-save notice
    await user.click(screen.getAllByRole("button", { name: /^Check this place/ })[0]!);
    await screen.findByText("Canary Cafe <b>Name</b>");
    await user.click(screen.getByRole("button", { name: "Remind me" }));
    const card = within(await screen.findByLabelText("Set a reminder", { selector: "section" }));
    const [first] = backend.calls("POST", "/v1/actions/propose");
    expect(first!.headers.get("idempotency-key")).toBeTruthy();
    expect(first!.body).toMatchObject({ effect: "SET_REMINDER", plan_version: 1 }); // no plan yet: version 1
    expect(Date.parse((first!.body as { due_at: string }).due_at) - Date.now()).toBeGreaterThan(55 * 60_000);

    await user.selectOptions(card.getByRole("combobox"), "30"); // a new time is a new proposal
    await waitFor(() => expect(backend.calls("POST", "/v1/actions/propose")).toHaveLength(2));
    const [, second] = backend.calls("POST", "/v1/actions/propose");
    expect(second!.headers.get("idempotency-key")).not.toBe(first!.headers.get("idempotency-key"));
    expect(Date.parse((second!.body as { due_at: string }).due_at) - Date.now()).toBeLessThan(31 * 60_000);
    await waitFor(() => expect(backend.calls("POST", "/v1/actions/act-1/cancel")).toHaveLength(1));

    // the new proposal is a new card (new action id): look it up again
    const fresh = within(await screen.findByLabelText("Set a reminder", { selector: "section" }));
    await user.click(fresh.getByRole("button", { name: "Confirm" }));
    await waitFor(async () => expect(await store.getAll("reminders")).toHaveLength(1));
    const [saved] = await store.getAll("reminders");
    expect(Date.parse(saved!.due_at)).toBe(Date.parse(serverDue)); // the server's confirmed time wins
  });

  it("does not save when the user declines the notice, and says so", async () => {
    const store = freshStore();
    const backend = boardBackend({
      "GET /v1/workspace": json(workspace()),
      "POST /v1/agent/turns": turnWithIds(),
      "POST /v1/places/details": json({ schema_version: 1, kind: "place_details", places: [placeCard()] }),
      "POST /v1/actions/propose": json(pendingAction({ id: "act-2", effect: "SET_REMINDER" })),
      "POST /v1/actions/act-2/confirm": json({ schema_version: 1, kind: "confirm_action_response", action: pendingAction({ id: "act-2", effect: "SET_REMINDER" }) }),
    });
    const user = await renderWithDiscovery(backend, store, false);
    await user.click(screen.getAllByRole("button", { name: /^Check this place/ })[0]!);
    await screen.findByText("Canary Cafe <b>Name</b>");
    await user.click(screen.getByRole("button", { name: "Remind me" }));
    const card = within(await screen.findByLabelText("Set a reminder", { selector: "section" }));
    await user.click(card.getByRole("button", { name: "Confirm" }));
    await user.click(await card.findByRole("button", { name: "Do not save" }));
    expect(await card.findByText(/confirmed but not saved/)).toBeInTheDocument();
    expect(await store.getAll("reminders")).toEqual([]);
  });
});

describe("canary scan", () => {
  it("persists no names, addresses, coordinates, Maps URLs, nonces or notes after a full flow", async () => {
    mockGeolocation("ok");
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    const consoleSpies = (["log", "info", "warn", "error", "debug"] as const).map((m) => vi.spyOn(console, m));
    const store = freshStore();
    const backend = boardBackend({
      "GET /v1/me/consents": CONSENT_ON,
      "GET /v1/workspace": json(workspace()),
      "POST /v1/agent/turns": turnWithIds(),
      "POST /v1/places/details": json({ schema_version: 1, kind: "place_details", places: [placeCard({ attributions: ["Canary Attribution"] })] }),
      "POST /v1/routes/check": json({ schema_version: 1, kind: "route_check_response", route: routeCheck() }),
      "POST /v1/actions/propose": json(pendingAction()),
      "POST /v1/actions/act-1/confirm": json({ schema_version: 1, kind: "confirm_action_response", action: pendingAction(), handoff_url: MAPS_URL }),
      "POST /v1/outcomes": json({ schema_version: 1, kind: "outcome_ack", recorded: false }),
    });
    const user = await renderWithDiscovery(backend, store);
    await waitFor(() => screen.getByRole("button", { name: "Stop using my location" }));
    await user.click(screen.getAllByRole("button", { name: /^Check this place/ })[0]!);
    await screen.findByText("About 25 minutes by car, about 8 km.");
    await user.click(screen.getByRole("button", { name: "Open in Google Maps" }));
    const card = within(await screen.findByLabelText("Open in Google Maps", { selector: "section" }));
    await user.click(card.getByRole("button", { name: "Confirm" }));
    await card.findByText(/Opened in Google Maps/);
    expect(open).toHaveBeenCalledTimes(1);
    const checkin = within(screen.getByLabelText("Did you go?", { selector: "section" }));
    await user.click(checkin.getByRole("button", { name: "I went" }));
    await user.type(checkin.getByPlaceholderText("Optional note"), "SECRETNOTE");
    await user.click(checkin.getByRole("button", { name: "Send" }));
    await waitFor(async () => expect(await store.getAll("outing_log")).toHaveLength(1));

    const dump = JSON.stringify(await store.exportAll());
    for (const canary of [
      "Canary Cafe", "Canary Street", "Canary Attribution", "Noodle Bar", "Tea House", "vegetarian", "google.com", "maps", "query_place_id",
      "48.85", "2.29", "latitude", "longitude", "nonce-secret", "SECRETNOTE", "key-act-1", "Google Maps",
    ]) {
      expect(dump, canary).not.toContain(canary);
    }
    // only the place ID and the user's own label are kept
    expect(dump).toContain(PLACE_A);
    expect(dump).toContain("My pick 1");
    // no console output, no coordinates or IDs in any URL
    for (const spy of consoleSpies) expect(spy).not.toHaveBeenCalled();
    for (const r of backend.requests) expect(r.url).not.toMatch(/ChIJ|48\.|2\.29|Canary/);
  });
});
