import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { handoffUrlFrom, isPinnedMapsUrl, placeIdOf, routeSummary, freshness, localOutcome } from "../board/viewModels";
import { Chat } from "../components/Chat";
import { bootstrapBody, GROUNDED_TEXT, json, mockGeolocation, errorBody, usageView } from "./support";
import {
  blockWithIds, boardBackend, MAPS_URL, pendingAction, placeCard, planVersion, PLACE_A, renderWithDiscovery, routeCheck, turnWithIds, workspace,
} from "./boardSupport";

afterEach(() => vi.restoreAllMocks());

const CONSENT_ON = json({ consents: [{ id: "c1", purpose: "LOCATION_FOREGROUND", notice_version: "location-notice-1", processors: ["GOOGLE"], granted_at: "2026-10-17T12:00:00Z" }] });

describe("view models", () => {
  it("rounds the drive time and never claims precision", () => {
    expect(routeSummary(routeCheck({ duration_minutes: 23, distance_meters: 8200 }))).toBe("About 25 minutes by car, about 8 km.");
    expect(routeSummary(routeCheck({ duration_minutes: 3, distance_meters: 600 }))).toBe("Under 5 minutes by car, under 1 km.");
    expect(routeSummary(routeCheck({ mode: "WALK", duration_minutes: 12, distance_meters: 900 }))).toContain("on foot");
  });
  it("pins the handoff host and reads the typed field only", () => {
    expect(isPinnedMapsUrl(MAPS_URL)).toBe(true);
    for (const bad of ["http://www.google.com/maps/x", "https://evil.example/maps", "https://www.google.com.evil.example/maps", "https://www.google.com/search?q=x", "javascript:alert(1)", 5]) {
      expect(isPinnedMapsUrl(bad)).toBe(false);
    }
    const action = pendingAction();
    expect(handoffUrlFrom({ schema_version: 1, kind: "confirm_action_response", action, handoff_url: MAPS_URL } as never)).toBe(MAPS_URL);
    expect(handoffUrlFrom({ schema_version: 1, kind: "confirm_action_response", action, handoff_url: "https://evil.example/maps" } as never)).toBeNull();
  });
  it("takes the place ID from the typed source field, never from text or URLs", () => {
    const [first] = blockWithIds().places;
    expect(placeIdOf(first!)).toBe(PLACE_A);
    // a URL that looks like it holds an ID is never parsed: only the typed field counts
    expect(placeIdOf({ place_id: "", title: "x", attribution_url: "https://www.google.com/maps/place/ChIJurl", place_url: "https://www.google.com/maps/place/ChIJurl" })).toBeNull();
    expect(placeIdOf({ place_id: "ChIJtyped", title: "x", attribution_url: "https://www.google.com/maps/place/ChIJurl", place_url: "https://www.google.com/maps/place/ChIJurl" })).toBe("ChIJtyped");
  });
  it("labels old checks stale and maps outcomes", () => {
    expect(freshness(new Date(Date.now() - 5 * 60_000).toISOString(), Date.now()).stale).toBe(false);
    expect(freshness(new Date(Date.now() - 50 * 60_000).toISOString(), Date.now()).stale).toBe(true);
    expect(localOutcome("DID_NOT_GO")).toBe("SKIPPED");
  });
});

describe("Trip Board states", () => {
  it("shows loading, then the goal, assumptions, constraints, version and empty places", async () => {
    const backend = boardBackend({ "GET /v1/workspace": json(workspace()) });
    render(<Chat api={backend.api} boardApi={backend.boardApi} />);
    expect(screen.getByText("Loading your board...")).toBeInTheDocument();
    expect(await screen.findByText("Goal: Quiet dinner tonight")).toBeInTheDocument();
    expect(screen.getByText("party_size: 2")).toBeInTheDocument();
    expect(screen.getByText("dietary: vegetarian")).toBeInTheDocument();
    expect(screen.getByText("must have")).toBeInTheDocument();
    expect(screen.getByText("Plan version 3.")).toBeInTheDocument();
    expect(screen.getByText(/No places yet/)).toBeInTheDocument();
    expect(screen.getByText(/No times yet/)).toBeInTheDocument();
    expect(screen.getByText("Nothing waiting for your OK.")).toBeInTheDocument();
  });

  it("degrades to a fixed 'not available yet' state on a typed 501, never showing server text", async () => {
    const backend = boardBackend({ "GET /v1/workspace": json(errorBody("POLICY", "NOT_IMPLEMENTED", "SECRET SERVER WORDS"), 501) });
    render(<Chat api={backend.api} boardApi={backend.boardApi} />);
    expect(await screen.findByText("The Trip Board is not available yet. You can keep chatting.")).toBeInTheDocument();
    expect(screen.queryByText(/SECRET SERVER WORDS/)).toBeNull();
  });

  it("treats a 503 DISABLED the same way and offers retry for a real provider error", async () => {
    let n = 0;
    const backend = boardBackend({
      "GET /v1/workspace": () => (++n === 1 ? json(errorBody("PROVIDER", "PROVIDER_UNAVAILABLE"), 503) : json(workspace())),
    });
    render(<Chat api={backend.api} boardApi={backend.boardApi} />);
    expect(await screen.findByText("Something went wrong on our side. You can try again.")).toBeInTheDocument();
    await userEvent.click(within(screen.getByLabelText("Trip Board")).getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Goal: Quiet dinner tonight")).toBeInTheDocument();
    const disabled = boardBackend({ "GET /v1/workspace": json(errorBody("POLICY", "DISABLED"), 503) });
    render(<Chat api={disabled.api} boardApi={disabled.boardApi} />);
    expect((await screen.findAllByText("The Trip Board is not available yet. You can keep chatting.")).length).toBeGreaterThan(0);
  });

  it("shows the server's plan warnings as fixed copy and the plan history from the typed view", async () => {
    const backend = boardBackend({
      "GET /v1/workspace": json(workspace({ latest_plan: planVersion(3), warnings: ["ASSUMPTIONS_UNCONFIRMED", "ROUTE_UNVERIFIED"], plan_versions: [1, 2, 3] })),
    });
    render(<Chat api={backend.api} boardApi={backend.boardApi} />);
    expect(await screen.findByText("Plan version 3.")).toBeInTheDocument();
    expect(screen.getByText("Earlier versions: 1, 2.")).toBeInTheDocument();
    expect(screen.getByText(/Some assumptions are not confirmed yet/)).toBeInTheDocument();
    expect(screen.getByText("The route has not been checked.")).toBeInTheDocument();
  });

  it("says so when there is no plan version yet", async () => {
    const backend = boardBackend({ "GET /v1/workspace": json(workspace({ latest_plan: null, plan_versions: [], version: 2 })) });
    render(<Chat api={backend.api} boardApi={backend.boardApi} />);
    expect(await screen.findByText("No plan yet. Board version 2.")).toBeInTheDocument();
    expect(screen.getByText("No earlier versions.")).toBeInTheDocument();
  });

  it("shows the permission-denied copy on 401 and the unsupported warning", async () => {
    const denied = boardBackend({ "GET /v1/workspace": json(errorBody("POLICY"), 401) });
    render(<Chat api={denied.api} boardApi={denied.boardApi} />);
    expect(await screen.findByText(/You are not signed in to the service/)).toBeInTheDocument();
  });

  it("flags an unsupported workspace and an out-of-date fact", async () => {
    const stale = workspace({ status: "UNSUPPORTED" });
    stale.facts[0] = { ...stale.facts[0]!, expires_at: new Date(Date.now() - 60_000).toISOString() };
    const backend = boardBackend({ "GET /v1/workspace": json(stale) });
    render(<Chat api={backend.api} boardApi={backend.boardApi} />);
    expect(await screen.findByText(/not supported in this version/)).toBeInTheDocument();
    expect(screen.getByText("1 item on the board may be out of date.")).toBeInTheDocument();
    expect(screen.getAllByText("may be out of date").length).toBeGreaterThan(0);
  });

  it("edits a chip with the idempotency key and base version, and retries with the same key", async () => {
    let attempt = 0;
    const backend = boardBackend({
      "GET /v1/workspace": json(workspace()),
      "PATCH /v1/workspace": () => (++attempt === 1 ? new Error("net") : json(workspace({ version: 4, latest_plan: planVersion(4), plan_versions: [1, 2, 3, 4] }))),
    });
    render(<Chat api={backend.api} boardApi={backend.boardApi} />);
    const user = userEvent.setup();
    await screen.findByText("party_size: 2");
    await user.click(screen.getByRole("button", { name: "Edit party_size: 2" }));
    const input = screen.getByLabelText("Value for party_size");
    await user.clear(input);
    await user.type(input, "4");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/Could not reach the service/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByText("Plan version 4.");
    const patches = backend.calls("PATCH", "/v1/workspace");
    expect(patches).toHaveLength(2);
    expect(patches[0]!.headers.get("idempotency-key")).toBeTruthy();
    expect(patches[1]!.headers.get("idempotency-key")).toBe(patches[0]!.headers.get("idempotency-key"));
    expect(patches[0]!.body).toMatchObject({ base_version: 3, upsert_facts: [{ key: "party_size", value: 4, scope: "trip", hard: false }], remove_fact_ids: [] });
  });

  it("adds, removes and confirms chips, and shows a conflict with a reload path", async () => {
    let conflict = true;
    const backend = boardBackend({
      "GET /v1/workspace": json(workspace()),
      "PATCH /v1/workspace": () => (conflict ? json(errorBody("CONFLICT"), 409) : json(workspace({ version: 5, latest_plan: planVersion(5), plan_versions: [1, 2, 3, 4, 5] }))),
    });
    render(<Chat api={backend.api} boardApi={backend.boardApi} />);
    const user = userEvent.setup();
    await screen.findByText("party_size: 2");
    await user.click(screen.getByRole("button", { name: "Confirm party_size: 2" }));
    expect(await screen.findByText(/changed somewhere else/)).toBeInTheDocument();
    conflict = false;
    await user.click(screen.getByRole("button", { name: "Reload the board" }));
    await waitFor(() => expect(screen.queryByText(/changed somewhere else/)).toBeNull());
    await user.type(screen.getByLabelText("Constraint name"), "parking");
    await user.type(screen.getByLabelText("Constraint value"), "needed");
    await user.click(screen.getByRole("button", { name: "Add" }));
    await screen.findByText("Plan version 5.");
    await user.click(screen.getByRole("button", { name: "Remove dietary: vegetarian" }));
    await waitFor(() => expect(backend.calls("PATCH", "/v1/workspace")).toHaveLength(3));
    const last = backend.calls("PATCH", "/v1/workspace")[2]!;
    expect(last.body).toMatchObject({ remove_fact_ids: ["f2"], upsert_facts: [] });
  });
});

describe("pick, place card and route card", () => {
  it("leaves Google's block verbatim and puts the pick buttons after it, never between it and its sources", async () => {
    const backend = boardBackend({ "POST /v1/agent/turns": turnWithIds() });
    await renderWithDiscovery(backend);
    const block = document.querySelector(".discovery")!;
    expect(block.querySelector(".discovery-text")!.textContent).toBe(GROUNDED_TEXT);
    expect(block.querySelector("button")).toBeNull();
    const sources = block.querySelector(".discovery-sources")!;
    expect(sources.querySelectorAll("a")).toHaveLength(2);
    expect(Array.from(block.children).map((c) => c.className)).toEqual(["discovery-brand", "discovery-text", "discovery-sources", "discovery-meta"]);
    const buttons = screen.getAllByRole("button", { name: /^Check this place/ });
    expect(buttons).toHaveLength(2);
    for (const b of buttons) expect(b.closest(".discovery")).toBeNull();
    expect(block.closest(".assistant")!.nextElementSibling).toBe(document.querySelector(".picks"));
  });

  it("offers no pick button for a source whose typed place ID is empty", async () => {
    const support = await import("./support");
    const block = support.discoveryBlock();
    const blank = { ...block, places: block.places.map((p) => ({ ...p, place_id: "" })) };
    const backend = boardBackend({ "POST /v1/agent/turns": json(support.turnResponse({ discovery: blank })) });
    await renderWithDiscovery(backend);
    expect(screen.queryByRole("button", { name: /^Check this place/ })).toBeNull();
  });

  it("shows the place card as text with the Google Maps attribution right after the fields, and a rounded drive time", async () => {
    mockGeolocation("ok");
    const backend = boardBackend({
      "GET /v1/me/consents": CONSENT_ON,
      "GET /v1/workspace": json(workspace()),
      "POST /v1/agent/turns": turnWithIds(),
      "POST /v1/places/details": json({ schema_version: 1, kind: "place_details", places: [placeCard({ attributions: ["Data provider text"] })] }),
      "POST /v1/routes/check": json({ schema_version: 1, kind: "route_check_response", route: routeCheck() }),
    });
    const user = await renderWithDiscovery(backend);
    await waitFor(() => expect(screen.getByRole("button", { name: "Stop using my location" })).toBeInTheDocument());
    await user.click(screen.getAllByRole("button", { name: /^Check this place/ })[0]!);
    const name = await screen.findByText("Canary Cafe <b>Name</b>");
    expect(name.querySelector("b")).toBeNull(); // text, never HTML
    const fields = name.closest(".place-fields")!;
    expect(fields.nextElementSibling!.textContent).toBe("Google Maps");
    expect(fields.nextElementSibling!.nextElementSibling!.textContent).toBe("Data provider text");
    expect(within(fields as HTMLElement).getByText("12 Canary Street, Testville")).toBeInTheDocument();
    expect(within(fields as HTMLElement).getByText("Open now")).toBeInTheDocument();
    expect(within(fields as HTMLElement).getByText("Rated 4.5 out of 5")).toBeInTheDocument();
    expect(await screen.findByText("About 25 minutes by car, about 8 km.")).toBeInTheDocument();
    expect(screen.getAllByText("About 25 minutes by car, about 8 km.").length).toBeGreaterThan(0);
    // details requested by place ID only; the route carried the position in this one request
    expect(backend.calls("POST", "/v1/places/details")[0]!.body).toMatchObject({ place_ids: [PLACE_A] });
    expect(backend.calls("POST", "/v1/routes/check")[0]!.body).toMatchObject({ place_id: PLACE_A, mode: "DRIVE", location: { latitude: 48.8584 } });
    expect(backend.requests.every((r) => !r.url.includes(PLACE_A) && !r.url.includes("48.85"))).toBe(true);
  });

  it("without location consent shows the fixed prompt and opens the existing consent flow", async () => {
    const backend = boardBackend({
      "GET /v1/workspace": json(workspace()),
      "POST /v1/agent/turns": turnWithIds(),
      "POST /v1/places/details": json({ schema_version: 1, kind: "place_details", places: [placeCard()] }),
    });
    const user = await renderWithDiscovery(backend);
    await user.click(screen.getAllByRole("button", { name: /^Check this place/ })[0]!);
    await screen.findByText("Canary Cafe <b>Name</b>");
    expect(await screen.findByText(/To see the drive time, turn on your location/)).toBeInTheDocument();
    expect(backend.calls("POST", "/v1/routes/check")).toHaveLength(0);
    const board = within(screen.getByLabelText("Trip Board"));
    await user.click(board.getByRole("button", { name: "Use my location" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument(); // the existing notice
  });

  it("handles partial (route fails, card stays), provider error with retry, and the WALK beta warning", async () => {
    mockGeolocation("ok");
    let routeCalls = 0;
    const backend = boardBackend({
      "GET /v1/me/consents": CONSENT_ON,
      "POST /v1/agent/turns": turnWithIds(),
      "POST /v1/places/details": json({ schema_version: 1, kind: "place_details", places: [placeCard()] }),
      "POST /v1/routes/check": () => {
        routeCalls += 1;
        if (routeCalls === 1) return json(errorBody("PROVIDER", "PROVIDER_UNAVAILABLE", "RAW PROVIDER"), 503);
        return json({ schema_version: 1, kind: "route_check_response", route: routeCheck({ mode: "WALK", duration_minutes: 12, distance_meters: 900 }) });
      },
    });
    const user = await renderWithDiscovery(backend);
    await waitFor(() => screen.getByRole("button", { name: "Stop using my location" }));
    await user.click(screen.getAllByRole("button", { name: /^Check this place/ })[0]!);
    expect(await screen.findByText(/Some details could not be loaded/)).toBeInTheDocument();
    expect(screen.queryByText(/RAW PROVIDER/)).toBeNull();
    expect(screen.getByText("Canary Cafe <b>Name</b>")).toBeInTheDocument();
    const board = within(screen.getByLabelText("Trip Board"));
    await user.click(board.getAllByRole("button", { name: "Try again" })[0]!);
    expect(await screen.findByText(/Walking times are a beta estimate/)).toBeInTheDocument();
  });

  it("shows 'not available yet' for details on a typed 501,", async () => {
    const backend = boardBackend({ "POST /v1/agent/turns": turnWithIds() });
    const user = await renderWithDiscovery(backend);
    await user.click(screen.getAllByRole("button", { name: /^Check this place/ })[0]!);
    expect(await screen.findByText("This is not available yet.")).toBeInTheDocument();
  });

  it("labels an old place check as stale", async () => {
    const backend = boardBackend({
      "POST /v1/agent/turns": turnWithIds(),
      "POST /v1/places/details": json({ schema_version: 1, kind: "place_details", places: [placeCard({ fetched_at: new Date(Date.now() - 2 * 3600_000).toISOString() })] }),
    });
    const user = await renderWithDiscovery(backend);
    await user.click(screen.getAllByRole("button", { name: /^Check this place/ })[0]!);
    expect(await screen.findByText(/May be out of date, refresh before you go/)).toBeInTheDocument();
  });
});

describe("handoff through the pending action protocol", () => {
  async function setup(extra: Parameters<typeof boardBackend>[0] = {}, gated = false) {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    const backend = boardBackend({
      "GET /v1/workspace": json(workspace()),
      "POST /v1/agent/turns": turnWithIds(),
      "POST /v1/places/details": json({ schema_version: 1, kind: "place_details", places: [placeCard()] }),
      "POST /v1/actions/propose": json(pendingAction()),
      ...(gated ? { "POST /v1/session:bootstrap": json(bootstrapBody(usageView({ meter_state: "GATED", gate_reason: "TURNS_LIMIT", user_turns_used: 50 }))) } : {}),
      ...extra,
    });
    const user = userEvent.setup();
    render(<Chat api={backend.api} boardApi={backend.boardApi} />);
    await screen.findByText("Goal: Quiet dinner tonight");
    return { backend, user, open };
  }

  async function pickAndPropose(user: ReturnType<typeof userEvent.setup>, gated = false) {
    if (!gated) {
      await user.type(screen.getByLabelText("Your message"), "quiet dinner");
      await user.click(screen.getByRole("button", { name: "Send" }));
      await screen.findByText("Here is what I found nearby.");
    }
    await user.click(screen.getAllByRole("button", { name: /^Check this place/ })[0]!);
    await screen.findByText("Canary Cafe <b>Name</b>");
    await user.click(screen.getByRole("button", { name: "Open in Google Maps" }));
    return screen.findByLabelText("Open in Google Maps", { selector: "section" });
  }

  it("proposes, shows the server preview and expiry, confirms once, opens the pinned URL in a new tab", async () => {
    const { backend, user, open } = await setup({
      "POST /v1/actions/act-1/confirm": json({ schema_version: 1, kind: "confirm_action_response", action: pendingAction({ confirmed_at: new Date().toISOString() }), handoff_url: MAPS_URL }),
    });
    const card = within(await pickAndPropose(user));
    expect(card.getByText("Open this place in Google Maps for directions.")).toBeInTheDocument();
    expect(card.getByText("Expires in about 14 minutes.")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("nonce-secret-1");
    expect(backend.calls("POST", "/v1/actions/propose")[0]!.body).toMatchObject({ effect: "LAUNCH_HANDOFF", target: PLACE_A, workspace_id: "ws-1", plan_version: 3 });
    expect(open).not.toHaveBeenCalled(); // nothing happens until the explicit confirm
    await user.click(card.getByRole("button", { name: "Confirm" }));
    expect(await card.findByText(/Opened in Google Maps in a new tab/)).toBeInTheDocument();
    expect(open).toHaveBeenCalledWith(MAPS_URL, "_blank", "noopener,noreferrer");
    const link = card.getByRole("link", { name: "Open Google Maps again" });
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link).toHaveAttribute("target", "_blank");
    const confirms = backend.calls("POST", "/v1/actions/act-1/confirm");
    expect(confirms).toHaveLength(1);
    expect(confirms[0]!.body).toMatchObject({ nonce: "nonce-secret-1" });
    expect(confirms[0]!.headers.get("idempotency-key")).toBe("key-act-1");
    // the check-in is offered after a handoff
    expect(screen.getByLabelText("Did you go?", { selector: "section" })).toBeInTheDocument();
  });

  it("retries a failed confirm with the same key and nonce", async () => {
    let n = 0;
    const { backend, user, open } = await setup({
      "POST /v1/actions/act-1/confirm": () =>
        ++n === 1 ? new Error("net") : json({ schema_version: 1, kind: "confirm_action_response", action: pendingAction(), handoff_url: MAPS_URL }),
    });
    const card = within(await pickAndPropose(user));
    await user.click(card.getByRole("button", { name: "Confirm" }));
    expect(await card.findByText(/Could not reach the service|changed or expired/)).toBeTruthy();
    await user.click(card.getByRole("button", { name: "Confirm again" }));
    await card.findByText(/Opened in Google Maps/);
    const confirms = backend.calls("POST", "/v1/actions/act-1/confirm");
    expect(confirms).toHaveLength(2);
    expect(confirms[1]!.headers.get("idempotency-key")).toBe(confirms[0]!.headers.get("idempotency-key"));
    expect(confirms[1]!.body).toEqual(confirms[0]!.body);
    expect(open).toHaveBeenCalledTimes(1);
  });

  it("cancels without any side effect", async () => {
    const { backend, user, open } = await setup({
      "POST /v1/actions/act-1/cancel": json({ schema_version: 1, kind: "cancel_action_response", action_id: "act-1", cancelled: true }),
    });
    const card = within(await pickAndPropose(user));
    await user.click(card.getByRole("button", { name: "Cancel" }));
    expect(await card.findByText("Cancelled. Nothing was done.")).toBeInTheDocument();
    expect(backend.calls("POST", "/v1/actions/act-1/confirm")).toHaveLength(0);
    expect(open).not.toHaveBeenCalled();
  });

  it("disables Confirm once the action has expired", async () => {
    const { user, backend } = await setup({
      "POST /v1/actions/propose": json(pendingAction({ expires_at: new Date(Date.now() - 1000).toISOString() })),
    });
    const card = within(await pickAndPropose(user));
    expect(card.getByText("Expired. Ask again for a fresh preview.")).toBeInTheDocument();
    expect(card.getByRole("button", { name: "Confirm" })).toBeDisabled();
    expect(backend.calls("POST", "/v1/actions/act-1/confirm")).toHaveLength(0);
  });

  it("shows the expired/changed copy on a confirm conflict", async () => {
    const { user } = await setup({ "POST /v1/actions/act-1/confirm": json(errorBody("CONFLICT"), 409) });
    const card = within(await pickAndPropose(user));
    await user.click(card.getByRole("button", { name: "Confirm" }));
    expect(await card.findByText(/changed or expired/)).toBeInTheDocument();
  });

  it("never opens a link off the pinned host", async () => {
    const { user, open } = await setup({
      "POST /v1/actions/act-1/confirm": json({ schema_version: 1, kind: "confirm_action_response", action: pendingAction(), handoff_url: "https://evil.example/maps/x" }),
    });
    const card = within(await pickAndPropose(user));
    await user.click(card.getByRole("button", { name: "Confirm" }));
    expect(await card.findByText(/no Google Maps link came back/)).toBeInTheDocument();
    expect(open).not.toHaveBeenCalled();
    expect(card.queryByRole("link")).toBeNull();
  });

  it("confirms a server-proposed handoff from the workspace while GATED", async () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    const backend = boardBackend({
      "POST /v1/session:bootstrap": json(bootstrapBody(usageView({ meter_state: "GATED", gate_reason: "TURNS_LIMIT", user_turns_used: 50 }))),
      "GET /v1/workspace": json(workspace({ pending_actions: [pendingAction()] })),
      "POST /v1/actions/act-1/confirm": json({ schema_version: 1, kind: "confirm_action_response", action: pendingAction(), handoff_url: MAPS_URL }),
    });
    render(<Chat api={backend.api} boardApi={backend.boardApi} />);
    await waitFor(() => expect(screen.getByLabelText("Your message")).toBeDisabled());
    const card = within(await screen.findByLabelText("Open in Google Maps", { selector: "section" }));
    await userEvent.click(card.getByRole("button", { name: "Confirm" }));
    await card.findByText(/Opened in Google Maps/);
    expect(open).toHaveBeenCalledWith(MAPS_URL, "_blank", "noopener,noreferrer");
  });
});
