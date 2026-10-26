import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { IDBFactory } from "fake-indexeddb";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Chat } from "../components/Chat";
import { LOCAL_NOTICE_TEXT } from "../storage";
import { freshStore } from "./idb";
import { bootstrapBody, fakeBackend, json, mockGeolocation, turnResponse, usageView } from "./support";

const CANARIES = ["Noodle Bar", "Tea House", "vegetarian", "google.com/maps", "1sAbCd", "ZyXwVu", "Google Maps", "48.8584", "2.2945", "48.858", "latitude", "longitude"];

function setOnline(value: boolean) {
  Object.defineProperty(navigator, "onLine", { value, configurable: true });
  act(() => void window.dispatchEvent(new Event(value ? "online" : "offline")));
}
afterEach(() => setOnline(true));

async function type(text: string) {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Your message"), text);
  return user;
}
const send = (user: ReturnType<typeof userEvent.setup>) => user.click(screen.getByRole("button", { name: "Send" }));
const accept = (user: ReturnType<typeof userEvent.setup>) => user.click(screen.getByRole("button", { name: "OK, save on this device" }));

describe("local-first chat", () => {
  it("shows the saved-on-this-device notice first and saves nothing until it is accepted", async () => {
    const store = freshStore();
    const backend = fakeBackend({ "POST /v1/agent/turns": json(turnResponse()) });
    render(<Chat api={backend.api} local={store} />);
    expect(await screen.findByText(LOCAL_NOTICE_TEXT)).toBeInTheDocument();
    const user = await type("before accepting");
    await send(user);
    await screen.findByText("Here is what I found nearby.");
    expect((await store.exportAll()).stores.messages).toEqual([]);
    expect(await store.noticeState()).toBe("unset");
    await accept(user);
    await waitFor(async () => expect(await store.noticeState()).toBe("accepted"));
    expect(screen.queryByText(LOCAL_NOTICE_TEXT)).toBeNull();
  });

  it("canary: no Maps content or coordinates reach IndexedDB after a full flow with location", async () => {
    mockGeolocation("ok");
    const store = freshStore();
    const backend = fakeBackend({
      "POST /v1/me/consents": json({ id: "c1", purpose: "LOCATION_FOREGROUND", notice_version: "location-notice-1", processors: ["GOOGLE"], granted_at: "2026-10-17T12:00:00Z" }),
      "POST /v1/agent/turns": json(turnResponse()),
    });
    render(<Chat api={backend.api} local={store} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "OK, save on this device" }));
    await user.click(await screen.findByRole("button", { name: "Use my location" }));
    await user.click(screen.getByRole("button", { name: "Agree and use my location" }));
    await screen.findByRole("button", { name: "Stop using my location" });
    await user.type(screen.getByLabelText("Your message"), "quiet dinner near me");
    await send(user);
    // The discovery block is displayed live...
    await screen.findByText("Here is what I found nearby."); expect((await screen.findAllByText(/Noodle Bar/)).length).toBeGreaterThan(0);
    // ...and the turn really carried the position to the server.
    expect(JSON.stringify(backend.turns()[0]?.body)).toContain("48.8584");
    await waitFor(async () => expect((await store.getAll("messages")).length).toBe(2));
    const dump = JSON.stringify(await store.exportAll());
    for (const canary of CANARIES) expect(dump, canary).not.toContain(canary);
    expect(dump).toContain("quiet dinner near me");
    expect(dump).toContain("Here is what I found nearby.");
    // Every record in every store is scanned by the dump above; the keys are checked too.
    const bundle = await store.exportAll();
    expect(bundle.stores.messages.map((m) => m.role)).toEqual(["user", "assistant"]);
    expect(bundle.stores.messages[1]?.had_discovery).toBe(true);
  });

  it("restores the user's messages, server reply text and draft, but not the discovery block", async () => {
    const factory = new IDBFactory();
    const store = freshStore(factory);
    const backend = fakeBackend({ "POST /v1/agent/turns": json(turnResponse()) });
    const first = render(<Chat api={backend.api} local={store} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "OK, save on this device" }));
    await user.type(screen.getByLabelText("Your message"), "quiet dinner");
    await send(user);
    await screen.findByText("Here is what I found nearby."); await screen.findAllByText(/Noodle Bar/);
    await user.type(screen.getByLabelText("Your message"), "half-typed draft");
    await waitFor(async () => expect((await store.getAll("drafts"))[0]?.text).toBe("half-typed draft"), { timeout: 3000 });
    first.unmount();
    store.close();

    render(<Chat api={fakeBackend().api} local={freshStore(factory)} />);
    expect(await screen.findByText("quiet dinner")).toBeInTheDocument();
    expect(screen.getByText("Here is what I found nearby.")).toBeInTheDocument();
    expect(screen.getByText(/Results are not stored; ask again/)).toBeInTheDocument();
    expect(screen.queryByText(/Noodle Bar/)).toBeNull();
    expect(screen.queryByText("Google Maps")).toBeNull();
    await waitFor(() => expect(screen.getByLabelText("Your message")).toHaveValue("half-typed draft"));
  });

  it("offline: shows the banner, queues with a persisted key, then flushes once with the same key", async () => {
    const store = freshStore();
    const backend = fakeBackend({ "POST /v1/agent/turns": json(turnResponse()) });
    render(<Chat api={backend.api} local={store} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "OK, save on this device" }));
    setOnline(false);
    expect(screen.getByText(/You are offline/)).toBeInTheDocument();
    await user.type(screen.getByLabelText("Your message"), "coffee later");
    await send(user);
    expect((await screen.findAllByText(/waiting to send/)).length).toBeGreaterThan(0);
    expect(backend.turns()).toHaveLength(0);
    const queued = await waitFor(async () => {
      const rows = await store.getAll("outbox");
      expect(rows).toHaveLength(1);
      return rows[0]!;
    });
    setOnline(true);
    await screen.findByText("Here is what I found nearby.");
    expect(backend.turns()).toHaveLength(1);
    expect(backend.turns()[0]?.headers.get("idempotency-key")).toBe(queued.key);
    await waitFor(async () => expect(await store.getAll("outbox")).toEqual([]));
    expect(screen.queryAllByText(/waiting to send/)).toEqual([]);
  });

  it("export and delete work while gated, and delete brings the notice back", async () => {
    const store = freshStore();
    await store.setNotice(true);
    await store.put("messages", { schema_version: 1, id: "m1", seq: 1, role: "user", text: "kept text", created_at: "t" });
    const gated = usageView({ meter_state: "GATED", gate_reason: "TRIPS_LIMIT", activated_trips_used: 10 });
    const backend = fakeBackend({ "POST /v1/session:bootstrap": json(bootstrapBody(gated)) });
    const createUrl = vi.fn(() => "blob:x");
    Object.defineProperty(URL, "createObjectURL", { value: createUrl, configurable: true });
    Object.defineProperty(URL, "revokeObjectURL", { value: vi.fn(), configurable: true });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);

    render(<Chat api={backend.api} local={store} />);
    const user = userEvent.setup();
    expect(await screen.findByText("kept text")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Your message")).toBeDisabled());
    await user.click(screen.getByRole("button", { name: "Export my data" }));
    expect(createUrl).toHaveBeenCalledTimes(1);
    const blob = (createUrl.mock.calls[0] as unknown as [Blob])[0];
    expect(await blob.text()).toContain("kept text");

    await user.click(screen.getByRole("button", { name: "Delete my data" }));
    await user.click(screen.getByRole("button", { name: "Yes, delete" }));
    await waitFor(() => expect(screen.queryByText("kept text")).toBeNull());
    for (const records of Object.values((await store.exportAll()).stores)) expect(records).toEqual([]);
    expect(await screen.findByText(LOCAL_NOTICE_TEXT)).toBeInTheDocument();
  });

  it("without IndexedDB the chat works in memory and shows no local controls", async () => {
    const backend = fakeBackend({ "POST /v1/agent/turns": json(turnResponse()) });
    render(<Chat api={backend.api} />);
    const user = await type("hello");
    await send(user);
    await screen.findByText("Here is what I found nearby.");
    expect(screen.queryByRole("button", { name: "Export my data" })).toBeNull();
  });
});
