import { IDBFactory } from "fake-indexeddb";
import { freshStore } from "./idb";
import { describe, expect, it } from "vitest";

import {
  NoticeRequiredError,
  SCHEMA_VERSION,
  StorageGuardError,
  type LocalStore,
} from "../storage";

const V = SCHEMA_VERSION;
const DAY = 86_400_000;

async function acceptedStore(): Promise<LocalStore> {
  const store = freshStore();
  await store.setNotice(true);
  return store;
}

const saved = (over: object = {}) => ({
  schema_version: V,
  id: "s1",
  place_id: "ChIJabc",
  label: "Home",
  kind: "HOME" as const,
  position: { lat: 48.858, lng: 2.295 },
  updated_at: "2026-10-26T00:00:00Z",
  ...over,
});

describe("local store write guard", () => {
  it("refuses a save before the first-save notice is accepted", async () => {
    const store = freshStore();
    await expect(
      store.put("drafts", { schema_version: V, id: "composer", text: "hi", updated_at: "x" }),
    ).rejects.toBeInstanceOf(NoticeRequiredError);
    expect(await store.noticeState()).toBe("unset");
    await store.setNotice(false);
    await expect(
      store.put("drafts", { schema_version: V, id: "composer", text: "hi", updated_at: "x" }),
    ).rejects.toBeInstanceOf(NoticeRequiredError);
  });

  it("round-trips every user store after the notice", async () => {
    const store = await acceptedStore();
    await store.put("messages", { schema_version: V, id: "m1", seq: 1, role: "user", text: "pizza", created_at: "t" });
    await store.put("plans", { schema_version: V, id: "p1", place_id: "ChIJabc", label: "Pizza", accepted_at: "t" });
    await store.put("reminders", { schema_version: V, id: "r1", place_id: "ChIJabc", label: "Pizza", due_at: "t", created_at: "t" });
    await store.put("outing_log", { schema_version: V, id: "o1", place_id: "ChIJabc", label: "Pizza", activity_tag: "dinner", at: new Date().toISOString(), outcome: "WENT" });
    await store.put("saved_places", saved());
    await store.put("ask_state", { schema_version: V, key: "Home", dont_ask_again: true, last_asked_at: null });
    expect((await store.getAll("saved_places"))[0]?.label).toBe("Home");
    expect((await store.get("plans", "p1"))?.place_id).toBe("ChIJabc");
  });

  it("rejects exact coordinates, unknown fields, wrong types and Maps-looking strings", async () => {
    const store = await acceptedStore();
    await expect(store.put("saved_places", saved({ position: { lat: 48.85837, lng: 2.294481 } }))).rejects.toBeInstanceOf(StorageGuardError);
    await expect(store.put("saved_places", saved({ position: { lat: 48.858, lng: 2.295, accuracy: 3 } }))).rejects.toBeInstanceOf(StorageGuardError);
    await expect(store.put("plans", { schema_version: V, id: "p", place_id: "x", label: "y", accepted_at: "t", address: "1 Rue X" } as never)).rejects.toBeInstanceOf(StorageGuardError);
    await expect(store.put("plans", { schema_version: V, id: "p", place_id: "x", label: "https://www.google.com/maps/place/x", accepted_at: "t" })).rejects.toBeInstanceOf(StorageGuardError);
    await expect(store.put("messages", { schema_version: V, id: "m", seq: "1", role: "user", text: "t", created_at: "t" } as never)).rejects.toBeInstanceOf(StorageGuardError);
    await expect(store.put("messages", { schema_version: V, id: "m", seq: 1, role: "system", text: "t", created_at: "t" } as never)).rejects.toBeInstanceOf(StorageGuardError);
    expect(await store.getAll("saved_places")).toEqual([]);
  });
});

describe("export, delete and retention", () => {
  it("exports everything as plain data and deleteItem removes one record", async () => {
    const store = await acceptedStore();
    await store.put("plans", { schema_version: V, id: "p1", place_id: "A", label: "One", accepted_at: "t" });
    await store.put("plans", { schema_version: V, id: "p2", place_id: "B", label: "Two", accepted_at: "t" });
    const bundle = await store.exportAll();
    expect(bundle.stores.plans.map((p) => p.id).sort()).toEqual(["p1", "p2"]);
    expect(JSON.parse(JSON.stringify(bundle)).schema_version).toBe(V);
    await store.deleteItem("plans", "p1");
    expect((await store.getAll("plans")).map((p) => p.id)).toEqual(["p2"]);
  });

  it("deleteAll clears every store and the notice flag", async () => {
    const store = await acceptedStore();
    await store.put("plans", { schema_version: V, id: "p1", place_id: "A", label: "One", accepted_at: "t" });
    await store.put("saved_places", saved());
    await store.deleteAll();
    const bundle = await store.exportAll();
    for (const records of Object.values(bundle.stores)) expect(records).toEqual([]);
    expect(await store.noticeState()).toBe("unset");
  });

  it("purges local outing_log entries past retention (default 90, ceiling 180, floor 30)", async () => {
    const store = await acceptedStore();
    const entry = (id: string, ageDays: number) => ({
      schema_version: V, id, place_id: "A", label: "L", activity_tag: "walk",
      at: new Date(Date.now() - ageDays * DAY).toISOString(), outcome: "WENT" as const,
    });
    await store.put("outing_log", entry("fresh", 10));
    await store.put("outing_log", entry("d100", 100));
    await store.put("outing_log", entry("d200", 200));
    expect(await store.retentionDays()).toBe(90);
    expect(await store.purgeExpired()).toBe(2);
    expect((await store.getAll("outing_log")).map((e) => e.id)).toEqual(["fresh"]);

    expect(await store.setRetentionDays(500)).toBe(180);
    expect(await store.setRetentionDays(1)).toBe(30);
    await store.setNotice(true);
    await store.put("outing_log", entry("d40", 40));
    await store.setRetentionDays(30);
    expect((await store.getAll("outing_log")).map((e) => e.id)).toEqual(["fresh"]);
  });

  it("purges on open", async () => {
    const factory = new IDBFactory();
    const first = freshStore(factory);
    await first.setNotice(true);
    await first.put("outing_log", {
      schema_version: V, id: "old", place_id: "A", label: "L", activity_tag: "walk",
      at: new Date(Date.now() - 120 * DAY).toISOString(), outcome: "WENT",
    });
    first.close();
    const second = freshStore(factory);
    expect(await second.getAll("outing_log")).toEqual([]);
  });
});
