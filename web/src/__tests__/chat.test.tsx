import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, vi } from "vitest";

import { DEV_TOKEN } from "../auth";
import { Chat } from "../components/Chat";
import { LOCATION_NOTICE_TEXT } from "../components/LocationControl";
import { errorBody, fakeBackend, json, mockGeolocation, turnResponse } from "./support";

const LAT = 48.8584;
const LON = 2.2945;

async function sendMessage(text = "a quiet dinner in Paris") {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Your message"), text);
  await user.click(screen.getByRole("button", { name: "Send" }));
  return user;
}

let geo: ReturnType<typeof mockGeolocation>;
beforeEach(() => {
  geo = mockGeolocation("ok");
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  localStorage.clear();
  sessionStorage.clear();
});

describe("sending and rendering", () => {
  it("starts with an empty state and no request but the consent lookup", async () => {
    const backend = fakeBackend();
    render(<Chat api={backend.api} />);
    expect(screen.getByText(/Ask about a place or an outing/)).toBeInTheDocument();
    await waitFor(() => expect(backend.requests.map((r) => r.url).sort()).toEqual(["/v1/me/consents", "/v1/session:bootstrap"]));
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("sends the dev token and a fresh idempotency key and renders the reply and Google's block", async () => {
    const backend = fakeBackend({ "POST /v1/agent/turns": json(turnResponse()) });
    render(<Chat api={backend.api} />);
    await sendMessage();
    expect(await screen.findByText("Here is what I found nearby.")).toBeInTheDocument();
    const [turn] = backend.turns();
    expect(turn?.headers.get("authorization")).toBe(`Bearer ${DEV_TOKEN}`);
    expect(turn?.headers.get("idempotency-key")).toMatch(/^web-/);
    expect(turn?.body).toEqual({ message: "a quiet dinner in Paris" });
    expect(screen.getByRole("link", { name: "Noodle Bar" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Google Maps results" })).toBeInTheDocument();
  });

  it("uses a different idempotency key for each new send", async () => {
    const backend = fakeBackend({ "POST /v1/agent/turns": json(turnResponse()) });
    render(<Chat api={backend.api} />);
    const user = await sendMessage("first");
    await screen.findAllByText("Here is what I found nearby.");
    await user.type(screen.getByLabelText("Your message"), "second");
    await user.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(backend.turns()).toHaveLength(2));
    const keys = backend.turns().map((t) => t.headers.get("idempotency-key"));
    expect(new Set(keys).size).toBe(2);
  });

  it("shows NEEDS_AREA as a plain reply, not an error", async () => {
    const reply = turnResponse({ reply: "Where should I look?", discovery: null, warnings: ["NEEDS_AREA"] });
    const backend = fakeBackend({ "POST /v1/agent/turns": json(reply) });
    render(<Chat api={backend.api} />);
    await sendMessage("dinner tonight");
    expect(await screen.findByText("Where should I look?")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows a loading state while waiting", async () => {
    let release: (r: Response) => void = () => undefined;
    const backend = fakeBackend({
      "POST /v1/agent/turns": () => new Promise<Response>((resolve) => (release = resolve)),
    });
    render(<Chat api={backend.api} />);
    await sendMessage();
    expect(await screen.findByText("Looking that up...")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    release(json(turnResponse()));
    await screen.findByText("Here is what I found nearby.");
    expect(screen.queryByText("Looking that up...")).toBeNull();
  });
});

describe("failure states", () => {
  async function failWith(response: Response, text?: RegExp) {
    const backend = fakeBackend({ "POST /v1/agent/turns": response });
    render(<Chat api={backend.api} />);
    await sendMessage();
    const alert = await screen.findByRole("alert");
    if (text) expect(alert).toHaveTextContent(text);
    return { backend, alert };
  }

  it("network failure offers a retry with the SAME idempotency key", async () => {
    let calls = 0;
    const backend = fakeBackend({
      "POST /v1/agent/turns": () => (++calls === 1 ? new TypeError("offline") : json(turnResponse())),
    });
    render(<Chat api={backend.api} />);
    const user = await sendMessage();
    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not reach the service/);
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByText("Here is what I found nearby.");
    const keys = backend.turns().map((t) => t.headers.get("idempotency-key"));
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("provider error is plain and retryable with the same key", async () => {
    const { backend } = await failWith(
      json(errorBody("PROVIDER", "PROVIDER_UNAVAILABLE"), 503),
      /Something went wrong on our side/,
    );
    expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
    expect(backend.turns()).toHaveLength(1);
  });

  it("rate limited shows the Retry-After seconds and holds the retry button until then", async () => {
    await failWith(
      json(errorBody("QUOTA", "RATE_LIMITED"), 429, { "Retry-After": "30" }),
      /Try again in 30 seconds/,
    );
    expect(screen.getByRole("button", { name: /Try again in 30s/ })).toBeDisabled();
  });

  it("rate limited without Retry-After still reads plainly", async () => {
    await failWith(json(errorBody("QUOTA", "RATE_LIMITED"), 429), /too quickly/);
  });

  it("allowance or quota exhaustion (429 QUOTA) explains the limit and offers no retry", async () => {
    await failWith(json(errorBody("QUOTA"), 429), /usage limit/);
    expect(screen.queryByRole("button", { name: /Try again/ })).toBeNull();
  });

  it("an over-budget turn asks for a simpler message", async () => {
    await failWith(json(errorBody("QUOTA", "OVER_BUDGET"), 429), /shorter or simpler/);
  });

  it.each([401, 403])("%i says the user is not signed in and offers no retry", async (status) => {
    await failWith(json(errorBody("POLICY"), status), /not signed in/);
    expect(screen.queryByRole("button", { name: /Try again/ })).toBeNull();
  });

  it("an expired replay asks for a fresh request and retries with a NEW key", async () => {
    let calls = 0;
    const backend = fakeBackend({
      "POST /v1/agent/turns": () =>
        ++calls === 1 ? json(errorBody("VALIDATION", "RESULT_EXPIRED"), 500) : json(turnResponse()),
    });
    render(<Chat api={backend.api} />);
    const user = await sendMessage();
    await screen.findByRole("alert");
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByText("Here is what I found nearby.");
    const keys = backend.turns().map((t) => t.headers.get("idempotency-key"));
    expect(keys[0]).not.toBe(keys[1]);
  });

  it("a request already in progress (409) can be retried", async () => {
    await failWith(json(errorBody("CONFLICT", "TURN_IN_PROGRESS"), 409), /still running/);
    expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
  });

  it("does not display the server's error message text", async () => {
    await failWith(json(errorBody("PROVIDER", undefined, "SECRET-INTERNAL-DETAIL"), 500));
    expect(screen.queryByText(/SECRET-INTERNAL-DETAIL/)).toBeNull();
  });
});

describe("location consent flow", () => {
  async function consented(extra: Record<string, never> = {}) {
    const backend = fakeBackend({
      "POST /v1/me/consents": json({ id: "c1", purpose: "LOCATION_FOREGROUND", notice_version: "location-notice-1", processors: ["GOOGLE"], granted_at: "2026-10-17T12:00:00Z" }),
      "DELETE /v1/me/consents/LOCATION_FOREGROUND": new Response(null, { status: 204 }),
      "POST /v1/agent/turns": json(turnResponse()),
      ...extra,
    });
    render(<Chat api={backend.api} />);
    const user = userEvent.setup();
    return { backend, user };
  }

  it("shows the placeholder notice first and calls nothing until the user agrees", async () => {
    const { backend, user } = await consented();
    await user.click(await screen.findByRole("button", { name: "Use my location" }));
    expect(screen.getByText(/PLACEHOLDER NOTICE \(location-notice-1\), pending counsel review \(issue #27\)/)).toBeInTheDocument();
    expect(screen.getByText(LOCATION_NOTICE_TEXT)).toBeInTheDocument();
    expect(backend.requests.some((r) => r.method === "POST" && r.url !== "/v1/session:bootstrap")).toBe(false);
    expect(geo.getCurrentPosition).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Use my location" })).toBeInTheDocument();
    expect(backend.requests.some((r) => r.method === "POST" && r.url !== "/v1/session:bootstrap")).toBe(false);
  });

  it("records consent, then asks the browser, then sends the position with the turn", async () => {
    const { backend, user } = await consented();
    await user.click(await screen.findByRole("button", { name: "Use my location" }));
    await user.click(screen.getByRole("button", { name: "Agree and use my location" }));
    expect(await screen.findByRole("button", { name: "Stop using my location" })).toBeInTheDocument();
    const grant = backend.requests.find((r) => r.method === "POST" && r.url === "/v1/me/consents");
    expect(grant?.body).toEqual({ purpose: "LOCATION_FOREGROUND", notice_version: "location-notice-1" });
    expect(grant?.headers.get("authorization")).toBe(`Bearer ${DEV_TOKEN}`);
    // The permission check never reaches the server.
    expect(backend.turns()).toHaveLength(0);

    await user.type(screen.getByLabelText("Your message"), "coffee near me");
    await user.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText("Here is what I found nearby.");
    expect(backend.turns()[0]?.body).toEqual({
      message: "coffee near me",
      location: { latitude: LAT, longitude: LON },
    });
    expect(geo.watchPosition).not.toHaveBeenCalled();
  });

  it("Stop using my location calls DELETE and later sends carry no location", async () => {
    const { backend, user } = await consented();
    await user.click(await screen.findByRole("button", { name: "Use my location" }));
    await user.click(screen.getByRole("button", { name: "Agree and use my location" }));
    await user.click(await screen.findByRole("button", { name: "Stop using my location" }));
    expect(await screen.findByRole("button", { name: "Use my location" })).toBeInTheDocument();
    expect(backend.requests.some((r) => r.method === "DELETE" && r.url === "/v1/me/consents/LOCATION_FOREGROUND")).toBe(true);
    await user.type(screen.getByLabelText("Your message"), "tea in Paris");
    await user.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText("Here is what I found nearby.");
    expect(backend.turns()[0]?.body).toEqual({ message: "tea in Paris" });
  });

  it("shows Stop when the server already holds an active consent", async () => {
    const backend = fakeBackend({
      "GET /v1/me/consents": json({ consents: [{ id: "c1", purpose: "LOCATION_FOREGROUND", notice_version: "location-notice-1", processors: ["GOOGLE"], granted_at: "2026-10-17T12:00:00Z" }] }),
    });
    render(<Chat api={backend.api} />);
    expect(await screen.findByRole("button", { name: "Stop using my location" })).toBeInTheDocument();
    expect(geo.getCurrentPosition).not.toHaveBeenCalled();
  });

  it("when the browser denies location, withdraws the consent and explains", async () => {
    geo = mockGeolocation("denied");
    const { backend, user } = await consented();
    await user.click(await screen.findByRole("button", { name: "Use my location" }));
    await user.click(screen.getByRole("button", { name: "Agree and use my location" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/browser blocked location access/);
    expect(backend.requests.some((r) => r.method === "DELETE")).toBe(true);
    expect(screen.getByRole("button", { name: "Use my location" })).toBeInTheDocument();
  });

  it("when the position is unavailable, says so and does not send the message", async () => {
    const { backend, user } = await consented();
    await user.click(await screen.findByRole("button", { name: "Use my location" }));
    await user.click(screen.getByRole("button", { name: "Agree and use my location" }));
    await screen.findByRole("button", { name: "Stop using my location" });
    geo = mockGeolocation("unavailable");
    await user.type(screen.getByLabelText("Your message"), "coffee near me");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/location isn't available|isn.t available/);
    expect(backend.turns()).toHaveLength(0);
  });

  it("when the consent call fails, says so and stays off", async () => {
    const { user } = await consented({ "POST /v1/me/consents": json(errorBody("POLICY"), 401) as never });
    await user.click(await screen.findByRole("button", { name: "Use my location" }));
    await user.click(screen.getByRole("button", { name: "Agree and use my location" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/not signed in/);
    expect(geo.getCurrentPosition).not.toHaveBeenCalled();
  });

  it("LOCATION_CONSENT_REQUIRED offers the consent flow and resends the held message after consent", async () => {
    let turnCalls = 0;
    const backend = fakeBackend({
      "POST /v1/agent/turns": () =>
        ++turnCalls === 1
          ? json(errorBody("POLICY", "LOCATION_CONSENT_REQUIRED"), 403)
          : json(turnResponse()),
      "POST /v1/me/consents": json({ id: "c1", purpose: "LOCATION_FOREGROUND", notice_version: "location-notice-1", processors: ["GOOGLE"], granted_at: "2026-10-17T12:00:00Z" }),
      "GET /v1/me/consents": json({ consents: [{ id: "c0", purpose: "LOCATION_FOREGROUND", notice_version: "location-notice-1", processors: ["GOOGLE"], granted_at: "2026-10-17T11:00:00Z" }] }),
    });
    render(<Chat api={backend.api} />);
    const user = userEvent.setup();
    await screen.findByRole("button", { name: "Stop using my location" });
    await user.type(screen.getByLabelText("Your message"), "coffee near me");
    await user.click(screen.getByRole("button", { name: "Send" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/needs your OK first/);
    await user.click(within(alert).getByRole("button", { name: "Use my location" }));
    await user.click(screen.getByRole("button", { name: "Agree and use my location" }));
    await screen.findByText("Here is what I found nearby.");
    const keys = backend.turns().map((t) => t.headers.get("idempotency-key"));
    expect(keys[0]).toBe(keys[1]);
    expect(backend.turns()[1]?.body).toEqual({ message: "coffee near me", location: { latitude: LAT, longitude: LON } });
  });
});

describe("privacy of coordinates and text", () => {
  it("never puts coordinates or message text in a URL, storage, console output or the page address", async () => {
    const consoleSpies = (["log", "info", "warn", "error", "debug"] as const).map((m) => vi.spyOn(console, m));
    const backend = fakeBackend({
      "POST /v1/me/consents": json({ id: "c1", purpose: "LOCATION_FOREGROUND", notice_version: "location-notice-1", processors: ["GOOGLE"], granted_at: "2026-10-17T12:00:00Z" }),
      "POST /v1/agent/turns": json(turnResponse()),
    });
    render(<Chat api={backend.api} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Use my location" }));
    await user.click(screen.getByRole("button", { name: "Agree and use my location" }));
    await screen.findByRole("button", { name: "Stop using my location" });
    await user.type(screen.getByLabelText("Your message"), "secret-message-text");
    await user.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText("Here is what I found nearby.");

    for (const request of backend.requests) {
      expect(request.url).not.toMatch(/48\.85|2\.29|secret-message-text|latitude|longitude/);
    }
    expect(window.location.href).not.toMatch(/48\.85|2\.29|secret-message-text/);
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    for (const spy of consoleSpies) expect(spy).not.toHaveBeenCalled();
  });

  it("keeps Google's text out of browser storage", async () => {
    const backend = fakeBackend({ "POST /v1/agent/turns": json(turnResponse()) });
    render(<Chat api={backend.api} />);
    await sendMessage();
    await screen.findByText("Here is what I found nearby.");
    expect(JSON.stringify({ ...localStorage })).not.toContain("Noodle");
    expect(sessionStorage.length).toBe(0);
  });

  it("source never uses console, web storage, IndexedDB, watchPosition or the URL for data", () => {
    const files: string[] = [];
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const path = join(dir, name);
        if (statSync(path).isDirectory()) {
          if (name !== "__tests__") walk(path);
        } else if (/\.(ts|tsx)$/.test(name) && !name.endsWith(".d.ts")) files.push(path);
      }
    };
    walk(join(__dirname, ".."));
    expect(files.length).toBeGreaterThan(5);
    for (const file of files) {
      const source = readFileSync(file, "utf8");
      // IndexedDB is allowed only inside the guarded storage module (src/storage/).
      const idb = file.replaceAll("\\", "/").includes("/src/storage/") ? "" : "indexedDB|";
      expect(source, file).not.toMatch(
        new RegExp(String.raw`console\.|localStorage|sessionStorage|${idb}watchPosition|history\.(push|replace)State|document\.cookie|dangerouslySetInnerHTML|innerHTML`),
      );
    }
  });
});
