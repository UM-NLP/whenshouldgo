import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { DiscoveryBlock } from "../api/client";
import { DevAuthProvider } from "../auth";
import { createBoardApi, type PendingAction, type PlaceCard, type RouteCheck, type Workspace } from "../board/boardApi";
import { Chat } from "../components/Chat";
import type { LocalStore } from "../storage";
import { discoveryBlock, errorBody, fakeBackend, json, turnResponse, type Handler } from "./support";

export const PLACE_A = "ChIJplaceAAAA";
export const PLACE_B = "ChIJplaceBBBB";
export const MAPS_URL = "https://www.google.com/maps/search/?api=1&query=x&query_place_id=ChIJplaceAAAA";

/** The default test block already carries typed place IDs (``PLACE_A`` then ``PLACE_B``). */
export function blockWithIds(): DiscoveryBlock {
  return discoveryBlock();
}

export function planVersion(version: number): NonNullable<Workspace["latest_plan"]> {
  return {
    schema_version: 1,
    kind: "plan_version",
    id: `ws-1.v${version}`,
    workspace_id: "ws-1",
    version,
    destination: { place_id: PLACE_A },
    origin_ref: "o0123456789abcdef01234",
    user_constraint_fact_ids: [],
    assumption_fact_ids: [],
    warnings: [],
    created_at: "2026-10-26T12:00:00Z",
  };
}

export function workspace(overrides: Partial<Workspace> = {}): Workspace {
  return {
    schema_version: 1,
    kind: "workspace_view",
    id: "ws-1",
    version: 3,
    status: "PROPOSED",
    goal: "Quiet dinner tonight",
    facts: [
      { schema_version: 1, kind: "fact", id: "f1", key: "party_size", value: 2, scope: "trip", hard: false, confirmation: "UNCONFIRMED", source: "INFERENCE", depends_on: [], evidence_ids: [] },
      { schema_version: 1, kind: "fact", id: "f2", key: "dietary", value: "vegetarian", scope: "trip", hard: true, confirmation: "CONFIRMED", source: "USER_EXPLICIT", depends_on: [], evidence_ids: [] },
    ],
    pending_actions: [],
    latest_plan: planVersion(3),
    warnings: [],
    plan_versions: [1, 2, 3],
    ...overrides,
  };
}

export function placeCard(overrides: Partial<PlaceCard> = {}): PlaceCard {
  return {
    schema_version: 1,
    kind: "place_card",
    display_only: true,
    place_id: PLACE_A,
    display_name: "Canary Cafe <b>Name</b>",
    formatted_address: "12 Canary Street, Testville",
    open_now: true,
    rating: 4.5,
    attributions: [],
    fetched_at: new Date().toISOString(),
    ...overrides,
  };
}

export function routeCheck(overrides: Partial<RouteCheck> = {}): RouteCheck {
  return {
    schema_version: 1,
    kind: "route_check",
    display_only: true,
    place_id: PLACE_A,
    mode: "DRIVE",
    duration_minutes: 23,
    distance_meters: 8200,
    beta_warning: false,
    attributions: [],
    fetched_at: new Date().toISOString(),
    ...overrides,
  };
}

export function pendingAction(overrides: Partial<PendingAction> = {}): PendingAction {
  return {
    schema_version: 1,
    kind: "pending_action",
    id: "act-1",
    workspace_id: "ws-1",
    plan_version: 3,
    effect: "LAUNCH_HANDOFF",
    target: PLACE_A,
    preview: "Open this place in Google Maps for directions.",
    nonce: "nonce-secret-1",
    idempotency_key: "key-act-1",
    created_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 14 * 60_000).toISOString(),
    ...overrides,
  };
}

/** A backend with the board's routes. Unlisted routes answer 501 like the frozen stubs. */
export function boardBackend(routes: Record<string, Handler | Response> = {}) {
  const stub = () => json(errorBody("POLICY", "NOT_IMPLEMENTED"), 501);
  const backend = fakeBackend({
    "GET /v1/workspace": stub(),
    "PATCH /v1/workspace": stub(),
    "POST /v1/places/details": stub(),
    "POST /v1/routes/check": stub(),
    "POST /v1/actions/propose": stub(),
    "POST /v1/outcomes": stub(),
    "POST /v1/me/consents": json({ id: "c1", purpose: "LOCATION_FOREGROUND", notice_version: "location-notice-1", processors: ["GOOGLE"], granted_at: "2026-10-17T12:00:00Z" }),
    ...routes,
  });
  const boardApi = createBoardApi({
    auth: new DevAuthProvider(),
    fetchImpl: backend.fetchImpl as unknown as typeof fetch,
    baseUrl: "http://localhost:5173",
  });
  const calls = (method: string, path: string) => backend.requests.filter((r) => r.method === method && r.url === path);
  return { ...backend, boardApi, calls };
}

/** Renders the chat with the board and sends one message so a discovery block is on screen. */
export async function renderWithDiscovery(
  backend: ReturnType<typeof boardBackend>,
  local: LocalStore | null = null,
  acceptNotice = true,
) {
  const user = userEvent.setup();
  render(<Chat api={backend.api} boardApi={backend.boardApi} local={local} />);
  if (local && acceptNotice) await user.click(await screen.findByRole("button", { name: "OK, save on this device" }));
  await user.type(screen.getByLabelText("Your message"), "quiet dinner");
  await user.click(screen.getByRole("button", { name: "Send" }));
  await screen.findByText("Here is what I found nearby.");
  return user;
}

export const turnWithIds = () => json(turnResponse({ discovery: blockWithIds() }));
