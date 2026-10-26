import { vi } from "vitest";

import {
  createApi,
  type DiscoveryBlock,
  type SessionBootstrap,
  type TurnResponse,
  type UsageView,
} from "../api/client";
import { DevAuthProvider } from "../auth";

export interface Recorded {
  method: string;
  url: string;
  headers: Headers;
  body: unknown;
}

export type Handler = (request: Recorded) => Response | Error | Promise<Response | Error>;

export function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

export function errorBody(code: string, reason?: string, message = "server message"): unknown {
  return { schema_version: 1, kind: "error", code, message, ...(reason ? { reason } : {}) };
}

/** A fake fetch. `routes` maps "METHOD /path" to a handler; GET consents defaults to none. */
export function fakeBackend(routes: Record<string, Handler | Response> = {}) {
  const requests: Recorded[] = [];
  const fetchImpl = vi.fn(async (input: Request): Promise<Response> => {
    const text = await input.clone().text();
    const url = new URL(input.url);
    const recorded: Recorded = {
      method: input.method,
      url: url.pathname + url.search,
      headers: input.headers,
      body: text ? JSON.parse(text) : undefined,
    };
    requests.push(recorded);
    const route = routes[`${input.method} ${url.pathname}`];
    if (route === undefined) {
      if (input.method === "GET" && url.pathname === "/v1/me/consents") return json({ consents: [] });
      return json(errorBody("VALIDATION"), 404);
    }
    const result = route instanceof Response ? route.clone() : await route(recorded);
    if (result instanceof Error) throw result;
    return result;
  });
  const api = createApi({
    auth: new DevAuthProvider(),
    fetchImpl: fetchImpl as unknown as typeof fetch,
    baseUrl: "http://localhost:5173",
  });
  return { api, requests, fetchImpl, turns: () => requests.filter((r) => r.url === "/v1/agent/turns") };
}

export const GROUNDED_TEXT =
  "**Noodle Bar** is a quiet spot with <b>vegetarian</b> options.\n\nTry the broth & the <script>alert(1)</script> tea.";

export function discoveryBlock(overrides: Partial<DiscoveryBlock> = {}): DiscoveryBlock {
  const token = "1sAbCdEfGh12:0x1a2b3c4d5e6f";
  const place = `https://www.google.com/maps/place//data=!4m2!3m1!${token}`;
  return {
    render_mode: "text",
    text: GROUNDED_TEXT,
    places: [
      { title: "Noodle Bar", attribution_url: place, place_url: place },
      {
        title: "Tea House",
        attribution_url: `https://www.google.com/maps/place//data=!4m2!3m1!1sZyXwVuTs98:0xaabbccdd1122`,
        place_url: `https://www.google.com/maps/place//data=!4m2!3m1!1sZyXwVuTs98:0xaabbccdd1122`,
      },
    ],
    branding: "Google Maps",
    translate_no: true,
    framing: "RESULTS_INTRO",
    retrieved_at: "2026-10-17T12:00:00Z",
    cache_expires_at: "2026-10-18T12:00:00Z",
    ...overrides,
  };
}

export function usageView(overrides: Partial<UsageView> = {}): UsageView {
  return {
    schema_version: 1,
    kind: "usage",
    period: "LIFETIME",
    user_turns_used: 0,
    user_turns_limit: 50,
    activated_trips_used: 0,
    activated_trips_limit: 10,
    resets_at: null,
    meter_state: "NONE",
    registered_turn_limit: 100,
    registered_trip_limit: 10,
    ...overrides,
  };
}

export function bootstrapBody(usage: UsageView = usageView()): SessionBootstrap {
  return {
    schema_version: 1,
    kind: "session_bootstrap",
    tier: "guest",
    policy_version: "p1",
    capabilities: {
      schema_version: 1,
      kind: "capabilities",
      can_start_turn: usage.meter_state !== "GATED",
      can_use_location: true,
      can_read: true,
      can_export: false,
      can_delete: false,
      can_link_account: true,
    },
    usage,
    server_time: "2026-10-22T12:00:00Z",
  };
}

export function turnResponse(overrides: Partial<TurnResponse> = {}): TurnResponse {
  return {
    schema_version: 1,
    kind: "turn_response",
    reply: "Here is what I found nearby.",
    domain: "TRAVEL",
    allowed_tools: [],
    workspace_id: null,
    allowance_exhausted: false,
    policy_version: "p1",
    usage: usageView(),
    discovery: discoveryBlock(),
    warnings: [],
    ...overrides,
  };
}

export function mockGeolocation(result: "ok" | "denied" | "unavailable" = "ok") {
  const getCurrentPosition = vi.fn(
    (success: PositionCallback, failure: PositionErrorCallback | null | undefined) => {
      if (result === "ok") {
        success({ coords: { latitude: 48.8584, longitude: 2.2945 } } as GeolocationPosition);
      } else {
        failure?.({ code: result === "denied" ? 1 : 2 } as GeolocationPositionError);
      }
    },
  );
  const watchPosition = vi.fn();
  Object.defineProperty(navigator, "geolocation", {
    value: { getCurrentPosition, watchPosition },
    configurable: true,
  });
  return { getCurrentPosition, watchPosition };
}
