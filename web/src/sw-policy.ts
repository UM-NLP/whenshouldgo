// The service worker's cache policy, kept free of worker globals so it can be unit tested.
// It caches the app shell (HTML, JS, CSS, icons) of THIS origin and nothing else: never an
// API response, never a cross-origin response (decision 27).

export const CACHE_PREFIX = "wsg-shell-";
export const SHELL_URLS = ["/", "/index.html"];

const SHELL_FILE = /\.(?:js|css|ico|png|svg|webmanifest|woff2)$/i;

export interface RequestLike {
  url: string;
  method: string;
  mode?: string;
}

export interface CacheLike {
  match(request: string | RequestLike): Promise<Response | undefined>;
  put(request: string | RequestLike, response: Response): Promise<void>;
  addAll?(urls: string[]): Promise<void>;
}
export interface CacheStorageLike {
  open(name: string): Promise<CacheLike>;
  keys(): Promise<string[]>;
  delete(name: string): Promise<boolean>;
}

export interface SwDeps {
  caches: CacheStorageLike;
  fetch(request: RequestLike): Promise<Response>;
  origin: string;
  version: string;
}

export const cacheName = (version: string) => `${CACHE_PREFIX}${version}`;

/** True only for a same-origin GET of an app-shell resource. */
export function isShellRequest(request: RequestLike, origin: string): boolean {
  if (request.method !== "GET") return false;
  let url: URL;
  try {
    url = new URL(request.url);
  } catch {
    return false;
  }
  if (url.origin !== origin) return false;
  const path = url.pathname;
  if (path === "/v1" || path.startsWith("/v1/") || path === "/healthz" || path === "/sw.js") return false;
  if (request.mode === "navigate") return true;
  return SHELL_URLS.includes(path) || path.startsWith("/assets/") || SHELL_FILE.test(path);
}

/** A response is stored only when it is an ordinary same-origin success that allows caching. */
export function isCacheable(response: Response): boolean {
  if (!response.ok || response.type === "opaque" || response.type === "cors") return false;
  const control = response.headers.get("cache-control") ?? "";
  return !/no-store/i.test(control);
}

/**
 * The fetch handler. Returns null to leave the request to the browser (anything that is not
 * the shell), otherwise a response promise: navigations are network-first with the cached
 * index as the offline fallback; other shell files are cache-first.
 */
export function respond(request: RequestLike, deps: SwDeps): Promise<Response> | null {
  if (!isShellRequest(request, deps.origin)) return null;
  const name = cacheName(deps.version);
  const navigation = request.mode === "navigate";
  const key = navigation ? "/index.html" : request;

  const fromNetwork = async (): Promise<Response> => {
    const response = await deps.fetch(request);
    if (isCacheable(response)) {
      const cache = await deps.caches.open(name);
      await cache.put(key, response.clone());
    }
    return response;
  };

  return (async () => {
    if (navigation) {
      try {
        return await fromNetwork();
      } catch {
        const cached = await (await deps.caches.open(name)).match(key);
        if (cached) return cached;
        throw new Error("offline and not cached");
      }
    }
    const cached = await (await deps.caches.open(name)).match(request);
    return cached ?? fromNetwork();
  })();
}

/** Install: precache the entry document. Assets are cached as the page requests them. */
export async function precache(deps: Pick<SwDeps, "caches" | "fetch" | "version">): Promise<void> {
  const cache = await deps.caches.open(cacheName(deps.version));
  const response = await deps.fetch({ url: "/index.html", method: "GET" });
  if (isCacheable(response)) await cache.put("/index.html", response);
}

/** Activate: delete every shell cache of another version. */
export async function cleanup(deps: Pick<SwDeps, "caches" | "version">): Promise<string[]> {
  const keep = cacheName(deps.version);
  const removed: string[] = [];
  for (const name of await deps.caches.keys()) {
    if (name.startsWith(CACHE_PREFIX) && name !== keep) {
      await deps.caches.delete(name);
      removed.push(name);
    }
  }
  return removed;
}
