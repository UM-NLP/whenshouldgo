# WhenShouldGo web (slice 1)

Mobile-first chat screen with a server-driven usage meter (bootstrap on load, updated from each turn; the client never computes entitlements): reply, Google's grounded block verbatim with its sources, and the
foreground location consent flow. Phase 0 only.

## Run (dev, dry run: no keys, no network)

Terminal 1, from `backend/`:

    python scripts/dev_server.py --i-understand-this-is-dev --dry-run

Terminal 2, from `web/`:

    npm install
    npm run dev          # http://127.0.0.1:5173, proxies /v1 to 127.0.0.1:8787

Live: replace the first command with
`python scripts/dev_server.py --i-understand-this-is-dev --env-file C:\path\to\.env --max-spend-usd 0.05`.

## Contract types

`npm run api:generate` exports the backend OpenAPI schema (`backend/scripts/export_openapi.py`)
and regenerates `src/api/schema.d.ts` with openapi-typescript. Re-run it after any backend
contract change and commit both generated files.

## Checks

    npm test
    npm run build

The dev auth provider and the dev server are NON-PRODUCTION. A production build gets a
`NoAuthProvider` and sends no token.


## Hosting headers (production)

Set these at the hosting layer (a `<meta>` CSP cannot set `frame-ancestors`, and a strict meta CSP
would break the Vite dev server's HMR, so none is baked into `index.html`):

    Content-Security-Policy: default-src 'self'; connect-src 'self'; frame-ancestors 'none'
    Permissions-Policy: geolocation=(self)

`connect-src 'self'` assumes the API is served from the same origin (or add its origin
explicitly). Google text is rendered as plain text and Google links are plain `https` anchors.


## Local-first storage, offline outbox and service worker (decisions 18, 19, 27)

- `src/storage/`: typed IndexedDB wrapper (`wsg-local`, `schema_version` per record, migration stub in
  `db.ts`). Stores: `messages`, `drafts`, `plans`, `reminders`, `outing_log`, `saved_places`,
  `ask_state`, plus `outbox` and `meta`. `schema.ts` is an allow-list write guard: unknown fields,
  Maps-looking strings and coordinates that are not rounded to 3 decimals are refused. No Maps content
  is stored: the user's messages and server-authored reply text only, with a `had_discovery` flag, so
  after a reload the UI says "Results are not stored; ask again". `exportAll()`, `deleteAll()`,
  `deleteItem()` and `purgeExpired()` (outing log, default 90 days, 30 to 180, purged on open) work
  at every meter state, including GATED.
- First-save notice: nothing is written until the user accepts "saved on this device only"
  (`useLocalNotice`); declining keeps everything in memory. Delete-all clears the flag too.
- `src/outbox/`: offline queue for turns. The idempotency key and the user's text are persisted (only
  after the notice); a consented location lives in memory only (the record has no location field,
  `location_dropped` is set). Replay is in order with the same key; 4xx other than 429 stops the queue
  with a typed state; 429 honors Retry-After; transient failures retry with the same key.
- `src/sw.ts` + `src/sw-policy.ts`: caches the same-origin app shell only (never `/v1`, never
  cross-origin, never `no-store` or failed responses), versioned cache with cleanup on activate. Built
  as `/sw.js` by `vite build` (second entry); registered only in production over HTTPS or localhost
  (`src/sw-register.ts`); an update shows a "Reload to update" banner. `npm run dev` registers nothing.
- Serve `/sw.js` with `Cache-Control: no-cache` and from the site root so its scope is `/`.
