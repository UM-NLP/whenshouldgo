/** A fresh idempotency key per send. Reused only for a retry of that same send. */
export function newIdempotencyKey(): string {
  const c = globalThis.crypto;
  if (typeof c?.randomUUID === "function") return `web-${c.randomUUID()}`;
  const bytes = new Uint8Array(16);
  c.getRandomValues(bytes);
  return `web-${Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")}`;
}
