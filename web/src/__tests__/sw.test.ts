import { describe, expect, it, vi } from "vitest";

import { canRegisterServiceWorker } from "../sw-register";
import { cacheName, cleanup, isShellRequest, precache, respond, type CacheLike, type CacheStorageLike, type RequestLike } from "../sw-policy";

const ORIGIN = "https://app.example";

function fakeCaches() {
  const stores = new Map<string, Map<string, Response>>();
  const keyOf = (r: string | RequestLike) => (typeof r === "string" ? r : new URL(r.url).pathname);
  const open = async (name: string): Promise<CacheLike> => {
    if (!stores.has(name)) stores.set(name, new Map());
    const m = stores.get(name)!;
    return {
      match: async (r) => m.get(keyOf(r))?.clone(),
      put: async (r, res) => void m.set(keyOf(r), res),
    };
  };
  const caches: CacheStorageLike = {
    open,
    keys: async () => [...stores.keys()],
    delete: async (n) => stores.delete(n),
  };
  return { caches, stores };
}

function deps(network: (r: RequestLike) => Response | Promise<Response>) {
  const { caches, stores } = fakeCaches();
  const fetch = vi.fn(async (r: RequestLike) => network(r));
  return { d: { caches, fetch, origin: ORIGIN, version: "v1" }, stores, fetch };
}
const req = (path: string, extra: Partial<RequestLike> = {}): RequestLike => ({ url: ORIGIN + path, method: "GET", ...extra });

describe("service worker cache policy", () => {
  it("never intercepts /v1, other methods or cross-origin requests", () => {
    for (const r of [
      req("/v1/agent/turns"),
      req("/v1/session:bootstrap"),
      req("/v1/me/consents"),
      req("/v1/x.js"),
      req("/healthz"),
      req("/sw.js"),
      { url: ORIGIN + "/assets/a.js", method: "POST" },
      { url: "https://maps.googleapis.com/assets/a.js", method: "GET" },
      { url: "https://cdn.example/app.css", method: "GET" },
      req("/some/page"),
    ]) {
      expect(isShellRequest(r, ORIGIN), r.url).toBe(false);
      const { d } = deps(() => new Response("x"));
      expect(respond(r, d), r.url).toBeNull();
    }
  });

  it("caches shell files and the navigation document, and serves them offline", async () => {
    const online = { on: true };
    const { d, stores } = deps((r) => {
      if (!online.on) throw new TypeError("offline");
      return new Response(`body:${new URL(r.url).pathname}`, { status: 200 });
    });
    await respond(req("/assets/main-abc.js"), d);
    await respond(req("/", { mode: "navigate" }), d);
    const cache = stores.get(cacheName("v1"))!;
    expect([...cache.keys()].sort()).toEqual(["/assets/main-abc.js", "/index.html"]);
    online.on = false;
    expect(await (await respond(req("/assets/main-abc.js"), d))!.text()).toBe("body:/assets/main-abc.js");
    expect(await (await respond(req("/anything", { mode: "navigate" }), d))!.text()).toBe("body:/");
  });

  it("does not store failures, no-store responses or non-basic responses", async () => {
    const make = (res: () => Response) => deps(() => res());
    for (const res of [
      () => new Response("x", { status: 500 }),
      () => new Response("x", { headers: { "cache-control": "no-store" } }),
    ]) {
      const { d, stores } = make(res);
      await respond(req("/assets/a.js"), d);
      expect([...(stores.get(cacheName("v1"))?.keys() ?? [])]).toEqual([]);
    }
  });

  it("precaches the index and removes caches of other versions on activate", async () => {
    const { d, stores } = deps(() => new Response("<html>"));
    stores.set("wsg-shell-old", new Map());
    stores.set("unrelated-cache", new Map());
    await precache(d);
    expect(await cleanup(d)).toEqual(["wsg-shell-old"]);
    expect([...stores.keys()].sort()).toEqual(["unrelated-cache", "wsg-shell-v1"]);
  });

  it("never caches any /v1 response even through a full session of requests", async () => {
    const { d, stores } = deps(() => new Response('{"reply":"Noodle Bar"}'));
    for (const p of ["/v1/agent/turns", "/v1/session:bootstrap"]) await respond(req(p), d);
    await respond(req("/assets/a.js"), d);
    const dump = [...stores.values()].flatMap((m) => [...m.keys()]).join(",");
    expect(dump).not.toContain("/v1");
  });
});

describe("service worker registration gate", () => {
  const base = { prod: true, protocol: "https:", hostname: "app.example", supported: true };
  it("only in production, over HTTPS or localhost, when supported", () => {
    expect(canRegisterServiceWorker(base)).toBe(true);
    expect(canRegisterServiceWorker({ ...base, prod: false })).toBe(false);
    expect(canRegisterServiceWorker({ ...base, supported: false })).toBe(false);
    expect(canRegisterServiceWorker({ ...base, protocol: "http:" })).toBe(false);
    expect(canRegisterServiceWorker({ ...base, protocol: "http:", hostname: "localhost" })).toBe(true);
    expect(canRegisterServiceWorker({ ...base, protocol: "http:", hostname: "192.168.1.5" })).toBe(false);
  });
});
