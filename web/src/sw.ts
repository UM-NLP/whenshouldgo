// Service worker entry (built to /sw.js). All policy lives in sw-policy.ts.
import { cleanup, precache, respond, type CacheStorageLike, type RequestLike } from "./sw-policy";

declare const __SW_VERSION__: string;

interface WorkerEvent extends Event {
  waitUntil(promise: Promise<unknown>): void;
}
interface FetchEventLike extends WorkerEvent {
  request: Request;
  respondWith(response: Promise<Response>): void;
}
interface MessageEventLike extends WorkerEvent {
  data: unknown;
}
interface WorkerScope {
  location: Location;
  caches: CacheStorageLike;
  skipWaiting(): Promise<void>;
  clients: { claim(): Promise<void> };
  addEventListener(type: string, listener: (event: never) => void): void;
}

const scope = self as unknown as WorkerScope;
const deps = () => ({
  caches: scope.caches,
  fetch: (request: RequestLike) => fetch(request as Request),
  origin: scope.location.origin,
  version: __SW_VERSION__,
});

scope.addEventListener("install", ((event: WorkerEvent) => {
  // No automatic skipWaiting: the page shows "update available" and asks for the swap.
  event.waitUntil(precache(deps()).catch(() => undefined));
}) as (event: never) => void);

scope.addEventListener("activate", ((event: WorkerEvent) => {
  event.waitUntil(cleanup(deps()).then(() => scope.clients.claim()));
}) as (event: never) => void);

scope.addEventListener("message", ((event: MessageEventLike) => {
  const data = event.data as { type?: string } | null;
  if (data?.type === "SKIP_WAITING") void scope.skipWaiting();
}) as (event: never) => void);

scope.addEventListener("fetch", ((event: FetchEventLike) => {
  const response = respond(event.request, deps());
  if (response) event.respondWith(response);
}) as (event: never) => void);
