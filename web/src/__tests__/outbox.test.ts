import { describe, expect, it, vi } from "vitest";

import type { Api, Outcome, TurnResponse } from "../api/client";
import type { Failure } from "../api/failures";
import { createOutbox, type OutboxItem } from "../outbox";
import { freshStore } from "./idb";
import { turnResponse } from "./support";

function fakeApi(results: Array<Outcome<TurnResponse>>) {
  const calls: Array<{ message: string; key: string; location: unknown }> = [];
  const queue = [...results];
  const sendTurn = vi.fn(async (input: { message: string; idempotencyKey: string; location?: unknown }) => {
    calls.push({ message: input.message, key: input.idempotencyKey, location: input.location ?? null });
    return queue.shift() ?? ({ ok: true, data: turnResponse() } as Outcome<TurnResponse>);
  });
  return { api: { sendTurn } as unknown as Pick<Api, "sendTurn">, calls };
}
const fail = (failure: Failure): Outcome<TurnResponse> => ({ ok: false, failure });
const ok = (): Outcome<TurnResponse> => ({ ok: true, data: turnResponse() });

async function setup(results: Array<Outcome<TurnResponse>>, now = () => 1_000_000) {
  const store = freshStore();
  await store.setNotice(true);
  const { api, calls } = fakeApi(results);
  const delivered: OutboxItem[] = [];
  const outbox = createOutbox({ store, api, now, onResult: (i) => delivered.push(i) });
  return { store, outbox, calls, delivered };
}

describe("offline outbox", () => {
  it("replays in order, each with the key it was queued under, exactly once", async () => {
    const { outbox, calls, delivered, store } = await setup([]);
    const a = await outbox.enqueue({ text: "first" });
    const b = await outbox.enqueue({ text: "second" });
    await Promise.all([outbox.flush(), outbox.flush()]);
    await outbox.flush();
    expect(calls.map((c) => [c.message, c.key])).toEqual([["first", a.key], ["second", b.key]]);
    expect(delivered.map((d) => d.text)).toEqual(["first", "second"]);
    expect(await store.getAll("outbox")).toEqual([]);
  });

  it("a transient failure keeps the item and retries with the SAME key", async () => {
    const { outbox, calls, delivered } = await setup([fail({ kind: "network" }), fail({ kind: "provider" })]);
    const item = await outbox.enqueue({ text: "hello" });
    await outbox.flush();
    await outbox.flush();
    expect(delivered).toEqual([]);
    await outbox.flush();
    expect(calls.map((c) => c.key)).toEqual([item.key, item.key, item.key]);
    expect(delivered).toHaveLength(1);
  });

  it("survives a reload: persisted keys replay after load() with the same key", async () => {
    const { store, outbox } = await setup([]);
    const item = await outbox.enqueue({ text: "later" });
    const { api, calls } = fakeApi([]);
    const reopened = createOutbox({ store, api, onResult: () => undefined });
    await reopened.load();
    await reopened.flush();
    expect(calls).toEqual([{ message: "later", key: item.key, location: null }]);
  });

  it("holds a location in memory only: never persisted, flagged location_dropped, sent this session", async () => {
    const { store, outbox, calls } = await setup([]);
    await outbox.enqueue({ text: "near me", location: { latitude: 48.8584, longitude: 2.2945 } });
    const dump = JSON.stringify(await store.exportAll());
    expect(dump).not.toMatch(/48\.8|2\.29|latitude|longitude/);
    expect((await store.getAll("outbox"))[0]?.location_dropped).toBe(true);
    await outbox.flush();
    expect(calls[0]?.location).toEqual({ latitude: 48.8584, longitude: 2.2945 });

    // After a reload the location is gone: the replay carries none.
    const { api, calls: later } = fakeApi([]);
    await outbox.enqueue({ text: "again", location: { latitude: 1.2345, longitude: 6.789 } });
    const reopened = createOutbox({ store, api, onResult: () => undefined });
    await reopened.load();
    await reopened.flush();
    expect(later[0]?.location).toBeNull();
  });

  it("stops on a 4xx other than 429, surfaces the typed state and blocks later items", async () => {
    const { outbox, calls, store } = await setup([fail({ kind: "invalid" })]);
    const a = await outbox.enqueue({ text: "bad" });
    await outbox.enqueue({ text: "behind" });
    await outbox.flush();
    expect(outbox.items().map((i) => [i.text, i.state, i.stopKind])).toEqual([["bad", "stopped", "invalid"], ["behind", "queued", null]]);
    await outbox.flush();
    expect(calls).toHaveLength(1);
    expect((await store.getAll("outbox")).find((r) => r.key === a.key)?.stop_kind).toBe("invalid");
    // The user discards it; the next item then goes.
    await outbox.discard(a.key);
    await outbox.flush();
    expect(calls.map((c) => c.message)).toEqual(["bad", "behind"]);
  });

  it("honors Retry-After on 429 and then retries with the same key", async () => {
    let t = 1_000_000;
    const { outbox, calls } = await setup([fail({ kind: "rate_limited", retryAfterSeconds: 20 })], () => t);
    const item = await outbox.enqueue({ text: "slow" });
    await outbox.flush();
    expect(outbox.items()[0]?.state).toBe("queued");
    expect(outbox.nextAttemptAt()).toBe(1_000_000 + 20_000);
    t += 5_000;
    await outbox.flush();
    expect(calls).toHaveLength(1);
    t += 16_000;
    await outbox.flush();
    expect(calls.map((c) => c.key)).toEqual([item.key, item.key]);
    expect(outbox.items()).toEqual([]);
  });

  it("resume gives an expired result a new key", async () => {
    const { outbox, calls } = await setup([fail({ kind: "result_expired" })]);
    const item = await outbox.enqueue({ text: "old" });
    await outbox.flush();
    const resumed = outbox.resume(item.key, { newKey: true });
    expect(resumed?.key).not.toBe(item.key);
    await outbox.flush();
    expect(calls[1]?.key).toBe(resumed?.key);
  });

  it("without the saved-on-device notice the queue works in memory and writes nothing", async () => {
    const store = freshStore();
    const { api, calls } = fakeApi([]);
    const outbox = createOutbox({ store, api, onResult: () => undefined });
    await outbox.enqueue({ text: "mem" });
    expect(await store.getAll("outbox")).toEqual([]);
    await outbox.flush();
    expect(calls).toHaveLength(1);
  });

  it("still delivers queued turns when the server says ok after an earlier result", async () => {
    const { outbox, delivered } = await setup([ok(), ok()]);
    await outbox.enqueue({ text: "1" });
    await outbox.enqueue({ text: "2" });
    await outbox.flush();
    expect(delivered.map((d) => d.text)).toEqual(["1", "2"]);
  });
});
