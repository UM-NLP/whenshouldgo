import { useEffect, useState } from "react";

/** Registration happens only in a production build and only over HTTPS or loopback. */
export function canRegisterServiceWorker(env: { prod: boolean; protocol: string; hostname: string; supported: boolean }): boolean {
  if (!env.prod || !env.supported) return false;
  return env.protocol === "https:" || env.hostname === "localhost" || env.hostname === "127.0.0.1" || env.hostname === "[::1]";
}

/** Registers /sw.js and calls `onUpdate(apply)` when a new version is waiting. */
export function registerServiceWorker(onUpdate: (apply: () => void) => void): void {
  const ok = canRegisterServiceWorker({
    prod: import.meta.env.PROD,
    protocol: globalThis.location?.protocol ?? "",
    hostname: globalThis.location?.hostname ?? "",
    supported: typeof navigator !== "undefined" && "serviceWorker" in navigator,
  });
  if (!ok) return;
  const announce = (worker: ServiceWorker) =>
    onUpdate(() => {
      worker.postMessage({ type: "SKIP_WAITING" });
      navigator.serviceWorker.addEventListener("controllerchange", () => globalThis.location.reload(), { once: true });
    });
  void navigator.serviceWorker
    .register("/sw.js", { scope: "/" })
    .then((registration) => {
      if (registration.waiting && navigator.serviceWorker.controller) announce(registration.waiting);
      registration.addEventListener("updatefound", () => {
        const worker = registration.installing;
        worker?.addEventListener("statechange", () => {
          if (worker.state === "installed" && navigator.serviceWorker.controller) announce(worker);
        });
      });
    })
    .catch(() => undefined);
}

/** The "update available" signal for the UI: a function that applies the update, or null. */
export function useServiceWorkerUpdate(): (() => void) | null {
  const [apply, setApply] = useState<(() => void) | null>(null);
  useEffect(() => {
    registerServiceWorker((fn) => setApply(() => fn));
  }, []);
  return apply;
}
