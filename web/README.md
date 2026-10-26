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


## Trip Board, place and route cards, handoff, reminders, check-in (Phase 0 journey)

Code: `src/board/` (typed API layer `boardApi.ts`, view-models `viewModels.ts`, local reminders
`reminders.ts`), `src/useBoard.ts`, `src/components/board/`. The chat gets the board only when a
`boardApi` is passed (`main.tsx` does); otherwise nothing is requested.

- Board: goal, assumptions (unconfirmed) and confirmed constraints as editable chips (edit, add, remove,
  one-tap "Confirm" on an assumption; the server stamps the confirmation). Edits are
  `PATCH /v1/workspace` with `base_version` and an `Idempotency-Key`; a retry reuses both; a 409 shows the
  conflict state with "Reload the board". Up to three places (place ID plus the user's own label, in memory),
  timeline, warnings, pending actions and the plan version. Older versions are not shown (no API yet).
- Pick: "Check this place" appears after Google's block (never inside it) and only for a source with a typed
  `place_id`. `POST /v1/places/details` renders name, address, open-now and rating as text with "Google Maps"
  immediately after the fields. `POST /v1/routes/check` (DRIVE) shows "About N minutes by car" (rounded to 5)
  only with location consent; otherwise a fixed prompt opens the existing consent flow. The position is read
  inside the tap, sent once, never kept. Card and route data live in memory only.
- Actions: the server's preview, Confirm / Cancel, expiry text. The nonce stays in memory and is never
  rendered. Confirm sends the action's own `idempotency_key`, so a retry repeats the same key and nonce.
  LAUNCH_HANDOFF opens the server-returned URL with `noopener,noreferrer` only if it is https on
  `www.google.com/maps` (also offered as a link), and works while the meter is GATED. SET_REMINDER saves
  place ID + the user's label + due time via the storage layer, after the first-save notice.
- Reminders: due and upcoming (next hour) reminders show as a banner when the app is open, with Snooze 10
  minutes, Dismiss and "Did you go?". A closed web tab cannot fire a reminder; nothing runs in the background.
- Check-in: WENT / DID_NOT_GO / CHANGED_PLAN plus an optional note, `POST /v1/outcomes` with a per-check-in key
  (reused on retry). If the ack says `recorded: false` (or the route is not available yet) the report is kept
  in the local `outing_log` (place ID and the user's label only). The note is never stored or logged.
- Every failure shows fixed copy chosen by kind; a typed 501/503 DISABLED means "not available yet".

### Contract gaps found (for the backend)

1. `DiscoveryPlace` has no `place_id`; the client reads `place_id` if the server adds it and shows no
   "Check this place" button otherwise. It never parses a URL or text.
2. `ConfirmActionResponse` has no handoff URL field; the client reads `handoff_url` (response or action),
   pinned to `https://www.google.com/maps`, and falls back to `action.target` only if that is a pinned URL.
3. `ProposeActionRequest` has no due time, so a reminder's time is chosen and kept on the device and the
   server preview cannot mention it.
4. `WorkspaceView` has no candidates, timeline, evidence freshness, warnings or plan history. Candidates are
   in memory; timeline reads facts keyed `leave_by` / `arrive_by`; freshness comes from the place and route
   `fetched_at`; history shows only the current version.
5. The board routes declare no `Idempotency-Key` header in OpenAPI; the client sends it anyway on PATCH
   workspace, confirm and outcomes.
6. `classifyFailure` maps 501/503 to "provider"; the board uses its own classifier (reason NOT_IMPLEMENTED or
   DISABLED, or status 501, is "unavailable").
